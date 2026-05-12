from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import join
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import (
    AssociatedFile,
    Dataset,
    IngestionRequest,
    ResourceACL,
)
from app.api.routes.gcs_storage import _DOWNLOAD_BUCKET, _get_gcs_storage_client
from app.utils.auth_utils import authorize_user, require_groups
from app.utils.sql_utils import only_update

router = APIRouter(tags=["ingestion_requests"])
engine = get_engine()


def _get_ir_with_read_access(session: Session, reqid: int, ag_ids: list) -> IngestionRequest:
    """Fetch an IngestionRequest and verify the caller has read access to its parent dataset."""
    ir = session.exec(
        select(IngestionRequest).where(IngestionRequest.id == reqid)
    ).one_or_none()
    if ir is None:
        raise HTTPException(status_code=404, detail="Ingestion request not found")

    ds = session.exec(
        select(Dataset)
        .select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True))
        .where(Dataset.unique_id == ir.dataset_mfid)
        .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True))
        .distinct()
    ).one_or_none()
    if ds is None:
        raise HTTPException(status_code=403, detail="Access denied")
    return ir


@router.get("/ingestion_requests", response_model=List[IngestionRequest])
def list_ingestion_requests(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    file_id: Optional[str] = None,
    dataset_id: Optional[str] = None,
):
    '''
    List ingestion requests. Either file_id or dataset_id must be provided.
    Requires read access to the parent dataset.
    '''
    if file_id is None and dataset_id is None:
        raise HTTPException(status_code=400, detail="Either file_id or dataset_id must be provided")

    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        if file_id is not None:
            af = session.exec(
                select(AssociatedFile).where(AssociatedFile.mfid == file_id)
            ).one_or_none()
            if af is None:
                raise HTTPException(status_code=404, detail="File not found")

            ds = session.exec(
                select(Dataset)
                .select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True))
                .where(Dataset.id == af.dataset_id)
                .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True))
                .distinct()
            ).one_or_none()
            if ds is None:
                raise HTTPException(status_code=403, detail="Access denied")

            return session.exec(
                select(IngestionRequest).where(IngestionRequest.file_mfid == af.mfid)
            ).all()

        else:
            ds = session.exec(
                select(Dataset)
                .select_from(join(Dataset, ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True))
                .where(Dataset.unique_id == dataset_id)
                .filter((ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True))
                .distinct()
            ).one_or_none()
            if ds is None:
                raise HTTPException(status_code=404, detail="Dataset not found or access denied")

            return session.exec(
                select(IngestionRequest).where(IngestionRequest.dataset_mfid == ds.unique_id)
            ).all()


@router.get("/ingestion_requests/{reqid}", response_model=IngestionRequest)
def get_ingestion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    reqid: int,
):
    '''Returns a specific ingestion request. Requires read access to the parent dataset.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        return _get_ir_with_read_access(session, reqid, ag_ids)


@router.patch("/ingestion_requests/{reqid}", response_model=IngestionRequest)
def patch_ingestion_request(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    reqid: int,
    ing_req: IngestionRequest,
):
    '''Updates an ingestion request. Admin/group access only.'''
    _, ag_list = auth_res
    require_groups(ag_list)

    if ing_req.id and ing_req.id != reqid:
        raise HTTPException(status_code=400, detail="Request body ID does not match URL")

    with Session(engine) as session:
        updated = only_update(
            session,
            IngestionRequest,
            IngestionRequest.id == reqid,
            **ing_req.model_dump(exclude_unset=True),
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="Ingestion request not found")

        if ing_req.status == "complete" and updated.file_mfid:
            af = session.exec(
                select(AssociatedFile).where(AssociatedFile.mfid == updated.file_mfid)
            ).one_or_none()
            if af is not None and af.storage_path is None:
                basename = af.filename.split("/")[-1]
                blob_name = f"{updated.dataset_mfid}/{basename}"
                try:
                    blob = _get_gcs_storage_client().bucket(_DOWNLOAD_BUCKET).blob(blob_name)
                    if blob.exists():
                        af.storage_path = f"{_DOWNLOAD_BUCKET}/{blob_name}"
                        session.add(af)
                except Exception:
                    pass  # non-fatal: lazy probe in download_link handles it

        session.commit()
        session.refresh(updated)
        return updated
