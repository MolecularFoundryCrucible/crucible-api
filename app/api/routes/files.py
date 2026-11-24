import json
import logging
from typing import Annotated, Literal, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.tables import (
    AssociatedFile,
    Dataset,
    IngestionRequest,
)
from app.config import get_secret, get_settings, require_feature
from app.general_utils import get_utc_isoformat
from app.utils.rmq_utils import send_pika_message, get_ingestion_tier
from app.utils.sql_utils import assert_not_pending_deletion, deletion_filter_for, file_named, offset_page, select_readable_file, touch_resource_modification_time
from app.models.datasets import AssociatedFileRead, AssociatedFileUpdate
from app.models.multipart import checked_filename
from app.models.ingestion import INGESTION_CLASS_PATTERN, IngestionRequestRead
from app.models.pagination import PaginatedResponse
from app.models.response_schemas import DownloadLinkRead, MessageResponse
from app.api.routes.gcs_storage import _ensure_storage_path, _signed_download
from app.api.errors import STORAGE_NOT_CONFIGURED_RESPONSE, STORE_UNAVAILABLE_RESPONSE
from app.services.file_versions import trash_file
from app.services.multipart import now, replace_in_progress
from app.services.storage_downloads import StorageLocationError, resolve_storage_object
from app.storage import store_config

from app.utils.auth_utils import authorize_user, permission_filter, require_permission
from app.utils.permissions import Principal, Permission

router = APIRouter(tags=["files"])


def _get_af_with_read_access(session: Session, file_id: str, principal) -> tuple[AssociatedFile, Dataset]:
    """Fetch an AssociatedFile by mfid with its dataset; 404 when missing or unreadable."""
    row = session.exec(select_readable_file(file_id, principal)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="File not found")
    return row


@router.get("/files", response_model=PaginatedResponse[AssociatedFileRead])
def list_files(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sha256_hash: Optional[str] = Query(default=None),
):
    '''Returns a paginated list of files the requesting user is authorized to view.'''

    with Session(get_engine()) as session:
        auth_filter = permission_filter(auth_res, Dataset, Permission.READ)
        base_query = (
            select(AssociatedFile)
            .join(Dataset, Dataset.id == AssociatedFile.dataset_id)
            .filter(auth_filter)
            .where(deletion_filter_for(Dataset))
            .options(selectinload(AssociatedFile.dataset))
        )
        if sha256_hash is not None:
            base_query = base_query.where(AssociatedFile.sha256_hash == sha256_hash)
        total, results = offset_page(session, base_query, limit, offset)
        items = [AssociatedFileRead.model_validate(af, update={"dataset_mfid": af.dataset.unique_id}) for af in results]
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/files/{file_id}", response_model=AssociatedFileRead, responses={200: {"headers": {"ETag": {"description": "Content ETag of the file, the same value as the `etag` field; send it as `If-Match` when replacing the file", "schema": {"type": "string"}}}}})
def get_file(auth_res: Annotated[Principal, Depends(authorize_user)], file_id: str, response: Response):
    '''Returns a single file by its MFID, with its content ETag in the ETag header.'''

    with Session(get_engine()) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)
        read = AssociatedFileRead.model_validate(af, update={"dataset_mfid": ds.unique_id})
        response.headers["ETag"] = read.etag
        return read


@router.get("/files/{file_id}/download_link", response_model=DownloadLinkRead, responses={**STORAGE_NOT_CONFIGURED_RESPONSE, **STORE_UNAVAILABLE_RESPONSE, 409: {"description": "Stored file location is outside the file's own dataset or an allowed bucket: `{\"detail\": {\"code\": \"storage_location_invalid\", \"reason\": ..., \"message\": ...}}`"}})
def get_file_download_link(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    file_id: str,
    disposition: Annotated[
        Literal["attachment", "inline"],
        Query(description="attachment (default) makes browsers save the file; inline asks them to display it when they can. Script-capable types (HTML, SVG, XML, +xml types, XSL) are always served as attachments."),
    ] = "attachment",
):
    '''Returns a signed GCS download URL (1-hour expiry) for a single file.
    Returns url=None for files whose storage_backend is not "gcs" (catalog-only).'''

    with Session(get_engine()) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)

        if af.storage_backend != "gcs":
            return {"url": None, "expires_in": None}

        generation = af.storage_generation
        filename = af.filename
        file_mfid = af.mfid
        store_id = af.store_id
        dataset_mfid = ds.unique_id
        storage_path = _ensure_storage_path(af, dataset_mfid, session)
        session.commit()

    url, expires_in = _signed_download(file_mfid, dataset_mfid, storage_path, generation, filename, disposition, store_id)
    return {"url": url, "expires_in": expires_in}


@router.patch("/files/{file_id}", response_model=AssociatedFileRead)
def update_file(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    file_id: str,
    updates: AssociatedFileUpdate,
):
    '''Partially updates a file record. Requires write access to the parent dataset.'''

    with Session(get_engine()) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)
        require_permission(session, auth_res, ds.unique_id, Permission.EDIT)
        assert_not_pending_deletion(session, ds.unique_id)

        changed = updates.model_dump(exclude_unset=True)
        if "storage_backend" in changed and (changed["storage_backend"] == "gcs") != (af.storage_backend == "gcs"):
            raise HTTPException(status_code=409, detail={"code": "storage_backend_class_change", "message": "A file cannot move between GCS and non-GCS storage"})
        if "filename" in changed and changed["filename"] != af.filename:
            if af.storage_backend != "gcs":
                raise HTTPException(status_code=409, detail={"code": "filename_immutable", "message": "The filename of a non-GCS file is its identity and cannot change"})
            try:
                checked_filename(changed["filename"])
            except ValueError as error:
                raise HTTPException(status_code=422, detail=str(error)) from None
            taken = file_named(session, af.dataset_id, changed["filename"])
            if taken is not None:
                raise HTTPException(status_code=409, detail={"code": "filename_exists", "file_mfid": taken.mfid, "message": "Filename already exists"})
        if af.storage_generation is not None:
            target_backend = changed.get("storage_backend", af.storage_backend)
            target_path = changed.get("storage_path", af.storage_path)
            if target_backend != "gcs" or target_path is None:
                raise HTTPException(status_code=422, detail="storage_path is assigned by the upload API for GCS files")
        if changed.get("storage_backend", af.storage_backend) == "gcs":
            new_path = "storage_path" in changed and changed["storage_path"] not in (None, af.storage_path)
            switched_with_path = af.storage_backend != "gcs" and changed.get("storage_path", af.storage_path) is not None
            if new_path or switched_with_path:
                raise HTTPException(status_code=422, detail="storage_path is assigned by the upload API for GCS files")
            if any(name in changed and changed[name] != getattr(af, name) for name in ("size", "sha256_hash")):
                raise HTTPException(status_code=422, detail="size and sha256_hash are determined by the stored object for GCS files")
        if any(name in changed and changed[name] != getattr(af, name) for name in ("size", "sha256_hash", "storage_path", "storage_backend")):
            af.sha256_verified_at = None
            af.storage_crc32c = None
        if any(name in changed and changed[name] != getattr(af, name) for name in ("storage_path", "storage_backend")):
            af.storage_generation = None
        for field, value in changed.items():
            setattr(af, field, value)
        session.add(af)
        touch_resource_modification_time(session, ds.unique_id)
        session.commit()
        session.refresh(af)
        return AssociatedFileRead.model_validate(af, update={"dataset_mfid": ds.unique_id})


@router.delete("/files/{file_id}", response_model=MessageResponse, responses={409: {"description": "The dataset is pending deletion (`pending_deletion`), or a replacement of the file is in progress (`replace_in_progress`, with its `upload_id`)"}})
def delete_file(auth_res: Annotated[Principal, Depends(authorize_user)], file_id: str):
    '''Deletes a file and its ingestion requests. Requires admin on the parent dataset. Stored content
    is kept for FILE_VERSION_RETENTION_DAYS, during which the file can be restored with its ingestion
    requests (`POST /datasets/{dataset_mfid}/deleted_files/{file_mfid}/restore`).'''

    with Session(get_engine()) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)
        require_permission(session, auth_res, ds.unique_id, Permission.DELETE)
        assert_not_pending_deletion(session, ds.unique_id)
        # Locked and read again: a replace completing meanwhile repoints the file, and its new object must be trashed.
        af = session.exec(select(AssociatedFile).where(AssociatedFile.id == af.id).with_for_update().execution_options(populate_existing=True)).one_or_none()
        if af is None:
            raise HTTPException(status_code=404, detail="File not found")
        replacing = replace_in_progress(session, af.mfid)
        if replacing is not None:
            raise HTTPException(status_code=409, detail={"code": "replace_in_progress", "upload_id": replacing.upload_id, "message": "A replacement of this file is in progress; abort it first"})
        trash_file(session, af, ds.unique_id, now())

        for ir in session.exec(
            select(IngestionRequest).where(IngestionRequest.file_mfid == af.mfid)
        ).all():
            session.delete(ir)

        session.delete(af)
        touch_resource_modification_time(session, ds.unique_id)
        session.commit()
        return {"message": f"File {file_id} deleted"}


# ingestion
@router.post("/files/{file_id}/ingest", response_model=IngestionRequestRead, responses={501: {"description": "Ingestion is not enabled on this server (`feature_disabled`, only for `status=requested`) or file storage is not configured (`storage_not_configured`)"}, 503: {"description": "The request could not be queued (`ingestion_unavailable`, recorded as failed), or the file's store is not available (`store_unavailable`, with `store_id`)"}, 409: {"description": "Stored file location is outside the file's own dataset or an allowed bucket: `{\"detail\": {\"code\": \"storage_location_invalid\", \"reason\": ..., \"message\": ...}}`"}})
def create_file_ingestion_request(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    file_id: str,
    ingestion_class: Optional[str] = Query(None, pattern=INGESTION_CLASS_PATTERN, description="An ingestor name, such as one from GET /ingestors"),
    status: Literal['requested', 'not_requested'] = 'requested'
):
    '''
    Creates an ingestion request for a file, scoped to the specified dataset.
    Returns error if the dataset associated with the file is not found, or the user does not have access.
    Requires write access to the dataset.

    Only 'requested' (dispatch to the ingestion queue) and 'not_requested' (record
    the file without dispatching) may be set here. Terminal states are written
    afterwards via PATCH /ingestion_requests/{request_id}.
    '''

    if status == "requested":
        require_feature("ingestion")

    with Session(get_engine()) as session:
        af, ds = _get_af_with_read_access(session, file_id, auth_res)
        require_permission(session, auth_res, ds.unique_id, Permission.CREATE)
        assert_not_pending_deletion(session, ds.unique_id)

        if af.storage_backend != "gcs":
            raise HTTPException(
                status_code=400,
                detail=f"Ingestion is not supported for storage_backend='{af.storage_backend}' files.",
            )

        _ensure_storage_path(af, ds.unique_id, session)
        try:
            resolve_storage_object(store_config(af.store_id), af.storage_path, af.storage_generation, ds.unique_id)
        except StorageLocationError as error:
            logger.warning("file_location_rejected file=%s dataset=%s reason=%s", af.mfid, ds.unique_id, error.reason)
            raise HTTPException(status_code=409, detail=error.detail()) from None
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
        af_generation = af.storage_generation
        af_store_id = af.store_id
        af_filename = af.filename
        ds_unique_id = ds.unique_id

    if status == 'requested':
        tier = get_ingestion_tier(af_size)
        routing_key = f'ingestion-{get_settings().ingestion.routing_suffix}-{tier}'
        try:
            send_pika_message(
                message=json.dumps({
                    "filename": af_storage_path,
                    "store_id": af_store_id,
                    **({"storage_generation": af_generation} if af_generation else {}),
                    "file_name": af_filename,
                    "reqid": ir_id,
                    "dsid": ds_unique_id,
                    "ingestion_class": ingestion_class,
                }),
                exchange="",
                routing_key=routing_key,
                rmq_pw=get_secret("RABBITMQ_DEFAULT_PW"),
            )
        except Exception:
            logger.exception("ingestion_publish_failed request=%s file=%s", ir_id, file_id)
            with Session(get_engine()) as session:
                failed = session.get(IngestionRequest, ir_id)
                failed.status = "failed"
                session.add(failed)
                session.commit()
            raise HTTPException(status_code=503, detail={"code": "ingestion_unavailable", "message": "Could not queue the ingestion request; try again later", "request_id": ir_id}) from None

    with Session(get_engine()) as session:
        return session.exec(select(IngestionRequest).where(IngestionRequest.id == ir_id)).one()
