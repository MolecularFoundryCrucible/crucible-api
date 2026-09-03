import os
import json
import logging
from datetime import timedelta
from typing import Annotated, List, Literal, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, join
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import (
    AssociatedFile,
    Dataset,
    IngestionRequest,
)
from app.general_utils import get_secret, get_utc_isoformat
from app.utils.rmq_utils import send_pika_message, get_ingestion_tier
from app.utils.sql_utils import touch_resource_modification_time
from app.models.datasets import AssociatedFileRead, AssociatedFileUpdate
from app.models.pagination import PaginatedResponse
from app.api.routes.gcs_storage import _ensure_storage_path, _generate_signed_url_link

from app.utils.auth_utils import authorize_user, permission_filter, require_permission
from app.utils.permissions import Principal, Permission

router = APIRouter(tags=["files"])
engine = get_engine()


def _get_af_with_read_access(session: Session, file_id: str, principal) -> tuple[AssociatedFile, Dataset]:
    """Fetch an AssociatedFile by mfid and verify the caller has read access to its parent dataset."""
    af = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_id)).one_or_none()
    if af is None:
        raise HTTPException(status_code=404, detail="File not found")

    ds = session.exec(
        select(Dataset)
        .where(Dataset.id == af.dataset_id)
        .where(permission_filter(principal, Dataset, Permission.READ))
    ).one_or_none()
    if ds is None:
        raise HTTPException(status_code=403, detail="Access denied")
    return af, ds


@router.get("/files", response_model=PaginatedResponse[AssociatedFileRead])
def list_files(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sha256_hash: Optional[str] = Query(default=None),
):
    '''Returns a paginated list of files the requesting user is authorized to view.'''

    with Session(engine) as session:
        auth_filter = permission_filter(auth_res, Dataset, Permission.READ)
        base_query = (
            select(AssociatedFile)
            .join(Dataset, Dataset.id == AssociatedFile.dataset_id)
            .filter(auth_filter)
            .options(selectinload(AssociatedFile.dataset))
        )
        if sha256_hash is not None:
            base_query = base_query.where(AssociatedFile.sha256_hash == sha256_hash)
        total = session.exec(
            select(func.count()).select_from(base_query.subquery())
        ).one()
        results = session.exec(base_query.offset(offset).limit(limit)).all()
        items = [AssociatedFileRead.model_validate(af, update={"dataset_mfid": af.dataset.unique_id}) for af in results]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/files/{file_id}", response_model=AssociatedFileRead)
def get_file(auth_res: Annotated[Principal, Depends(authorize_user)], file_id: str):
    '''Returns a single file by its MFID.'''

    with Session(engine) as session:
        af, _ = _get_af_with_read_access(session, file_id, auth_res)
        return AssociatedFileRead.model_validate(af, update={"dataset_mfid": af.dataset.unique_id})


@router.get("/files/{file_id}/download_link")
def get_file_download_link(auth_res: Annotated[Principal, Depends(authorize_user)], file_id: str):
    '''Returns a signed GCS download URL (1-hour expiry) for a single file.
    Returns url=None for files whose storage_backend is not "gcs" (catalog-only).'''

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)

        if af.storage_backend != "gcs":
            return {"url": None, "expires_in": None}

        storage_path = _ensure_storage_path(af, ds.unique_id, session)
        session.commit()

    url, expires_in = _generate_signed_url_link(storage_path)
    return {"url": url, "expires_in": expires_in}


@router.patch("/files/{file_id}", response_model=AssociatedFileRead)
def update_file(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    file_id: str,
    updates: AssociatedFileUpdate,
):
    '''Partially updates a file record. Requires write access to the parent dataset.'''

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)
        require_permission(session, auth_res, ds.unique_id, Permission.EDIT)

        for field, value in updates.model_dump(exclude_unset=True).items():
            setattr(af, field, value)
        session.add(af)
        touch_resource_modification_time(session, ds.unique_id)
        session.commit()
        session.refresh(af)
        return AssociatedFileRead.model_validate(af, update={"dataset_mfid": af.dataset.unique_id})


@router.delete("/files/{file_id}")
def delete_file(auth_res: Annotated[Principal, Depends(authorize_user)], file_id: str):
    '''Deletes a file and its ingestion requests. Requires admin on the parent dataset.'''

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)
        require_permission(session, auth_res, ds.unique_id, Permission.DELETE)

        for ir in session.exec(
            select(IngestionRequest).where(IngestionRequest.file_mfid == af.mfid)
        ).all():
            session.delete(ir)

        session.delete(af)
        touch_resource_modification_time(session, ds.unique_id)
        session.commit()
        return {"message": f"File {file_id} deleted"}


# ingestion
@router.post("/files/{file_id}/ingest", response_model=IngestionRequest)
def create_file_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    file_id: str,
    ingestion_class: Optional[str] = None,
    status: Literal['requested', 'not_requested'] = 'requested'
):
    '''
    Creates an ingestion request for a file, scoped to the specified dataset.
    Returns error if the dataset associated with the file is not found, or the user does not have access.
    Requires write access to the dataset.

    Only 'requested' (dispatch to the ingestion queue) and 'not_requested' (record
    the file without dispatching) may be set here. Terminal states are written
    afterwards via PATCH /ingestion_requests/{reqid}.
    '''

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)
        require_permission(session, auth_res, ds.unique_id, Permission.CREATE)

        if af.storage_backend != "gcs":
            raise HTTPException(
                status_code=400,
                detail=f"Ingestion is not supported for storage_backend='{af.storage_backend}' files.",
            )

        RMQ_ROUTING_SUFFIX = os.getenv("RMQ_ROUTING_SUFFIX", None)
        ingestion_request = IngestionRequest(
            dataset_mfid=ds.unique_id,
            file_mfid=af.mfid,
            file_to_upload=af.storage_path,
            ingestion_class=ingestion_class,
            time_created=get_utc_isoformat(),
            status=status,
        )
        session.add(ingestion_request)
        session.commit()
        session.refresh(ingestion_request)

        ir_id = ingestion_request.id
        af_size = af.size
        af_storage_path = af.storage_path
        ds_unique_id = ds.unique_id

    if status == 'requested':
        tier = get_ingestion_tier(af_size)
        routing_key = f'ingestion-{RMQ_ROUTING_SUFFIX}-{tier}'
        rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
        send_pika_message(
            message=json.dumps({
                "filename": af_storage_path,
                "reqid": ir_id,
                "dsid": ds_unique_id,
                "ingestion_class": ingestion_class,
            }),
            exchange="",
            routing_key=routing_key,
            user="admin",
            rmq_pw=rmq_pw,
        )

    with Session(engine) as session:
        return session.exec(select(IngestionRequest).where(IngestionRequest.id == ir_id)).one()
