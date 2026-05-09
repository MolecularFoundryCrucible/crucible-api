import json
import logging
import os
from typing import Annotated, List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, join
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_secret, get_tz_isoformat
from app.models.core_models import (
    AssociatedFile,
    Dataset,
    IngestionRequest,
    ResourceACL,
)
from app.models.datasets import AssociatedFileRead, AssociatedFileUpdate
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, require_groups, require_write_access
from app.utils.rmq_utils import send_pika_message
from app.utils.sql_utils import only_update

router = APIRouter(tags=["associated_files"])
engine = get_engine()

RMQ_ROUTING_SUFFIX = os.getenv("RMQ_ROUTING_SUFFIX", None)

_INGESTION_SMALL_MAX = int(1.5 * 1024**3)
_INGESTION_MEDIUM_MAX = 5 * 1024**3


def _get_ingestion_tier(file_size: int) -> str:
    if file_size < _INGESTION_SMALL_MAX:
        return "small"
    elif file_size < _INGESTION_MEDIUM_MAX:
        return "medium"
    return "large"


def _get_af_with_read_access(session: Session, mfid: str, ag_ids: list) -> tuple[AssociatedFile, Dataset]:
    """Fetch an AssociatedFile by unique_id and verify the caller has read access to its parent dataset."""
    af = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one_or_none()
    if af is None:
        raise HTTPException(status_code=404, detail="Associated file not found")

    ds = session.exec(
        select(Dataset)
        .select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True))
        .where(Dataset.id == af.dataset_id)
        .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True))
        .distinct()
    ).one_or_none()
    if ds is None:
        raise HTTPException(status_code=403, detail="Access denied")
    return af, ds


@router.get("/associated_files", response_model=PaginatedResponse[AssociatedFileRead])
def list_associated_files(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    '''Returns a paginated list of associated files the requesting user is authorized to view.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        base_joins = (
            select(AssociatedFile)
            .join(Dataset, Dataset.id == AssociatedFile.dataset_id)
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(auth_filter)
            .distinct()
        )
        total = session.exec(
            select(func.count()).select_from(base_joins.subquery())
        ).one()
        results = session.exec(base_joins.offset(offset).limit(limit)).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=results)


@router.get("/associated_files/{mfid}", response_model=AssociatedFileRead)
def get_associated_file(auth_res: Annotated[tuple, Depends(authorize_user)], mfid: str):
    '''Returns a single associated file by its MFID.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, _ = _get_af_with_read_access(session, mfid, ag_ids)
        return af


@router.patch("/associated_files/{mfid}", response_model=AssociatedFileRead)
def update_associated_file(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    mfid: str,
    updates: AssociatedFileUpdate,
):
    '''Partially updates an associated file. Requires write access to the parent dataset.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, mfid, ag_ids)
        require_write_access(session, ag_ids, [ds.unique_id])

        for field, value in updates.model_dump(exclude_unset=True).items():
            setattr(af, field, value)
        session.add(af)
        session.commit()
        session.refresh(af)
        return af


@router.delete("/associated_files/{mfid}")
def delete_associated_file(auth_res: Annotated[tuple, Depends(authorize_user)], mfid: str):
    '''Deletes an associated file and its ingestion requests. Requires write access to the parent dataset.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, mfid, ag_ids)
        require_write_access(session, ag_ids, [ds.unique_id])

        for ir in session.exec(
            select(IngestionRequest).where(IngestionRequest.associated_file_id == af.id)
        ).all():
            session.delete(ir)

        session.delete(af)
        session.commit()
        return {"message": f"Associated file {mfid} deleted"}


@router.post("/associated_files/{mfid}/ingest", response_model=IngestionRequest)
def create_ingestion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    mfid: str,
    ingestion_class: Optional[str] = None,
):
    '''Creates an ingestion request for the associated file and dispatches it to the RMQ queue.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, mfid, ag_ids)
        require_write_access(session, ag_ids, [ds.unique_id])

        ingestion_request = IngestionRequest(
            dataset_id=ds.id,
            associated_file_id=af.id,
            file_to_upload=af.filename,
            ingestion_class=ingestion_class,
            time_created=get_tz_isoformat(),
            status="requested",
        )
        session.add(ingestion_request)
        session.commit()
        session.refresh(ingestion_request)
        ir_id = ingestion_request.id

    tier = _get_ingestion_tier(af.size)
    routing_key = f'ingestion-{RMQ_ROUTING_SUFFIX}-{tier}'
    logger.info(f"Ingestion routing key: {routing_key}")
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    send_pika_message(
        message=json.dumps({
            "filename": af.filename,
            "reqid": ir_id,
            "dsid": ds.unique_id,
            "ingestion_class": ingestion_class,
        }),
        exchange="",
        routing_key=routing_key,
        user="admin",
        rmq_pw=rmq_pw,
    )

    with Session(engine) as session:
        return session.exec(select(IngestionRequest).where(IngestionRequest.id == ir_id)).one()


@router.get("/associated_files/{mfid}/ingest", response_model=List[IngestionRequest])
def list_ingestion_requests(auth_res: Annotated[tuple, Depends(authorize_user)], mfid: str):
    '''Lists all ingestion requests for an associated file.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, _ = _get_af_with_read_access(session, mfid, ag_ids)
        return session.exec(
            select(IngestionRequest).where(IngestionRequest.associated_file_id == af.id)
        ).all()


@router.get("/associated_files/{mfid}/ingest/{reqid}", response_model=IngestionRequest)
def get_ingestion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    mfid: str,
    reqid: int,
):
    '''Returns a specific ingestion request for an associated file.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, _ = _get_af_with_read_access(session, mfid, ag_ids)
        ir = session.exec(
            select(IngestionRequest).where(
                (IngestionRequest.id == reqid) & (IngestionRequest.associated_file_id == af.id)
            )
        ).one_or_none()
        if ir is None:
            raise HTTPException(status_code=404, detail="Ingestion request not found")
        return ir


@router.patch("/associated_files/{mfid}/ingest/{reqid}", response_model=IngestionRequest)
def patch_ingestion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    mfid: str,
    reqid: int,
    ing_req: IngestionRequest,
):
    '''Updates an ingestion request. Admin/group access only.'''
    _, ag_list = auth_res
    require_groups(ag_list)

    if ing_req.id and ing_req.id != reqid:
        raise HTTPException(status_code=400, detail="Request body ID does not match URL")

    with Session(engine) as session:
        af = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one_or_none()
        if af is None:
            raise HTTPException(status_code=404, detail="Associated file not found")

        updated = only_update(
            session,
            IngestionRequest,
            (IngestionRequest.id == reqid) & (IngestionRequest.associated_file_id == af.id),
            **ing_req.model_dump(exclude_unset=True),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Ingestion request not found")

        session.commit()
        session.refresh(updated)
        return updated
