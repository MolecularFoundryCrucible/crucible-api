from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.api.path_params import DatasetMfid
from app.models.datasets import AssociatedFileRead, DeletedFileRead
from app.models.pagination import PaginatedResponse
from app.models.response_schemas import MessageResponse
from app.services.file_versions import RestoreRefused, deleted_versions, purge_deleted, restore_deleted
from app.services.multipart import now
from app.storage import get_store, stored_in
from app.tables import FileVersion
from app.utils.auth_utils import authorize_user, require_permission
from app.utils.permissions import Permission, Principal
from app.utils.sql_utils import assert_not_pending_deletion, dataset_or_404, offset_page

router = APIRouter(tags=["files"])

REFUSALS = {
    "not_in_trash": (404, "No deleted file with this MFID is kept for this dataset"),
    "version_expired": (409, "The grace period has ended; the content is being removed"),
    "dataset_pending_deletion": (409, "The dataset is pending deletion"),
    "filename_exists": (409, "A file with this filename exists in the dataset; rename or delete it first"),
    "filename_reserved": (409, "An upload in progress uses this filename"),
    "version_missing_in_storage": (409, "The kept content is no longer in storage"),
    "version_size_mismatch": (409, "The kept content in storage does not match the deleted file"),
}
CODES = ", ".join(f"`{code}`" for code in REFUSALS)


def _refused(error: RestoreRefused) -> HTTPException:
    status, message = REFUSALS[error.code]
    return HTTPException(status_code=status, detail={"code": error.code, "message": message})


def _require_dataset_admin(session, principal, dsid):
    dataset_or_404(session, dsid)
    require_permission(session, principal, dsid, Permission.DELETE)


@router.get("/datasets/{dataset_mfid}/deleted_files", response_model=PaginatedResponse[DeletedFileRead])
def list_deleted_files(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    '''Deleted files of the dataset that can still be restored, newest first. Requires admin on the dataset.'''
    with Session(get_engine()) as session:
        _require_dataset_admin(session, auth_res, dsid)
        total, rows = offset_page(session, deleted_versions(dsid).where(FileVersion.delete_after > now()), limit, offset)
        items = [
            DeletedFileRead(file_mfid=version.file_mfid, dataset_mfid=version.dataset_mfid, filename=version.filename, size=version.size, sha256_hash=version.sha256_hash, store_id=stored_in(version.store_id), deleted_at=version.created_at, restorable_until=version.delete_after)
            for version in rows
        ]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.post("/datasets/{dataset_mfid}/deleted_files/{file_mfid}/restore", response_model=AssociatedFileRead, responses={404: {"description": "Dataset not found, or no deleted file with this MFID is kept (`not_in_trash`)"}, 409: {"description": f"`{{\"detail\": {{\"code\": ..., \"message\": ...}}}}` with one of {CODES}, or `pending_deletion`"}})
def restore_deleted_file(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, file_mfid: str):
    '''Restores a deleted file with its MFID, filename, content and ingestion requests, while its content is kept. Requires admin on the dataset.'''
    with Session(get_engine()) as session:
        _require_dataset_admin(session, auth_res, dsid)
        assert_not_pending_deletion(session, dsid)
    try:
        file = restore_deleted(get_engine(), get_store, dsid, file_mfid, now())
    except RestoreRefused as error:
        raise _refused(error) from None
    return AssociatedFileRead.model_validate(file, update={"dataset_mfid": dsid})


@router.delete("/datasets/{dataset_mfid}/deleted_files/{file_mfid}", response_model=MessageResponse, responses={404: {"description": "Dataset not found, or no deleted file with this MFID is kept (`not_in_trash`)"}, 409: {"description": "The dataset is pending deletion (`pending_deletion`)"}})
def purge_deleted_file(auth_res: Annotated[Principal, Depends(authorize_user)], dsid: DatasetMfid, file_mfid: str):
    '''Ends a deleted file's grace period now: it can no longer be restored, and the next storage cleanup removes its content. Requires admin on the dataset.'''
    with Session(get_engine()) as session:
        _require_dataset_admin(session, auth_res, dsid)
        assert_not_pending_deletion(session, dsid)
        try:
            purge_deleted(session, dsid, file_mfid, now())
        except RestoreRefused as error:
            raise _refused(error) from None
        session.commit()
    return {"message": f"Deleted file {file_mfid} will be removed by the next storage cleanup"}
