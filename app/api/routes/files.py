import logging
from datetime import timedelta
from typing import Annotated, List, Optional

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
    ResourceACL,
)
from app.models.datasets import AssociatedFileRead, AssociatedFileUpdate
from app.models.pagination import PaginatedResponse
from app.api.routes.gcs_storage import _DOWNLOAD_BUCKET, _get_gcs_storage_client
from app.utils.auth_utils import authorize_user, require_write_access

router = APIRouter(tags=["files"])
engine = get_engine()


def _get_af_with_read_access(session: Session, file_id: str, ag_ids: list) -> tuple[AssociatedFile, Dataset]:
    """Fetch an AssociatedFile by mfid and verify the caller has read access to its parent dataset."""
    af = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_id)).one_or_none()
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
    return af, ds


@router.get("/files", response_model=PaginatedResponse[AssociatedFileRead])
def list_files(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sha256_hash: Optional[str] = Query(default=None),
):
    '''Returns a paginated list of files the requesting user is authorized to view.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        auth_filter = (ResourceACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        base_query = (
            select(AssociatedFile)
            .join(Dataset, Dataset.id == AssociatedFile.dataset_id)
            .join(ResourceACL, ResourceACL.mfid == Dataset.unique_id, isouter=True)
            .filter(auth_filter)
            .options(selectinload(AssociatedFile.dataset))
            .distinct()
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
def get_file(auth_res: Annotated[tuple, Depends(authorize_user)], file_id: str):
    '''Returns a single file by its MFID.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, _ = _get_af_with_read_access(session, file_id, ag_ids)
        return AssociatedFileRead.model_validate(af, update={"dataset_mfid": af.dataset.unique_id})


@router.get("/files/{file_id}/download_link")
def get_file_download_link(auth_res: Annotated[tuple, Depends(authorize_user)], file_id: str):
    '''Returns a signed GCS download URL (1-hour expiry) for a single file.
    If storage_path is not set, attempts to locate the file in production storage
    by basename and persists the path for future calls.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, ag_ids)

        if not af.storage_path:
            basename = af.filename.split("/")[-1]
            ds_blobs = _get_gcs_storage_client().bucket(_DOWNLOAD_BUCKET).list_blobs(prefix = f'{ds.unique_id}/')
            for blob in ds_blobs:
                if blob.name.endswith(basename):
                    break
            else:
                raise HTTPException(
                    status_code=404,
                    detail="File not found in production storage — not yet ingested",
                )

            af.storage_path = f"{_DOWNLOAD_BUCKET}/{blob.name}"
            session.add(af)
            session.commit()
        else:
            bucket_name, _, blob_name = af.storage_path.partition("/")
            blob = _get_gcs_storage_client().bucket(bucket_name).blob(blob_name)

    try:
        url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=1), method="GET")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not generate signed URL: {e}")

    return {"url": url, "expires_in": 3600}


@router.patch("/files/{file_id}", response_model=AssociatedFileRead)
def update_file(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    file_id: str,
    updates: AssociatedFileUpdate,
):
    '''Partially updates a file record. Requires write access to the parent dataset.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, ag_ids)
        require_write_access(session, ag_ids, [ds.unique_id])

        for field, value in updates.model_dump(exclude_unset=True).items():
            setattr(af, field, value)
        session.add(af)
        session.commit()
        session.refresh(af)
        return AssociatedFileRead.model_validate(af, update={"dataset_mfid": af.dataset.unique_id})


@router.delete("/files/{file_id}")
def delete_file(auth_res: Annotated[tuple, Depends(authorize_user)], file_id: str):
    '''Deletes a file and its ingestion requests. Requires write access to the parent dataset.'''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        af, ds = _get_af_with_read_access(session, file_id, ag_ids)
        require_write_access(session, ag_ids, [ds.unique_id])

        for ir in session.exec(
            select(IngestionRequest).where(IngestionRequest.file_mfid == af.mfid)
        ).all():
            session.delete(ir)

        session.delete(af)
        session.commit()
        return {"message": f"File {file_id} deleted"}
