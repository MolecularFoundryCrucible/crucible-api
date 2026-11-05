import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from google.api_core.exceptions import NotFound
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import DatasetMfid
from app.config import get_settings
from app.models.core_models import AssociatedFile, Dataset, UploadSession
from app.models.datasets import AssociatedFileRead
from app.models.uploads import UploadComplete, UploadInitiate, UploadInitiateRead, UploadMultipartInitiateRead
from app.services import multipart
from app.api.errors import NOT_AVAILABLE_HERE
from app.services.storage_downloads import DOWNLOAD_LIFETIME, MULTIPART_SEGMENT, StorageLocationError, multipart_prefix, resolve_storage_object, upload_bucket
from app.storage import get_store
from app.storage.gcs import server_client, server_credentials, storage_scoped
from app.utils.auth_utils import authorize_user, require_permission
from app.utils.permissions import Principal, Permission
from app.utils.sql_utils import assert_not_pending_deletion, touch_resource_modification_time

router = APIRouter(tags=["storage"])
logger = logging.getLogger(__name__)

LEGACY_UPLOAD_DEPRECATION = "@1793318400"
LEGACY_UPLOAD_RESPONSE = {NOT_AVAILABLE_HERE: {"description": "File storage is not configured on this server (`storage_not_configured`), or it is not Google Cloud Storage, which these routes need (`legacy_upload_unavailable`; upload through `/upload/multipart` instead): `{\"detail\": {\"code\": ..., \"message\": ...}}`"}}

_GCS_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1/b"
_CHUNK_SIZE_HINT = 8 * 1024 * 1024  # 8 MiB — GCS recommended chunk multiple
_MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 500 * 1024 ** 3))  # default 500 GiB

_gcs_authed_session = None


def _get_gcs_authed_session():
    global _gcs_authed_session
    if _gcs_authed_session is None:
        from google.auth.transport.requests import AuthorizedSession
        scoped = storage_scoped(server_credentials())
        _gcs_authed_session = AuthorizedSession(scoped)
    return _gcs_authed_session


def _legacy_upload_bucket():
    """The upload bucket for the token-based upload routes, which only Google Cloud Storage supports."""
    bucket = upload_bucket()
    if get_settings().storage.backend != "gcs":
        raise HTTPException(status_code=NOT_AVAILABLE_HERE, detail={"code": "legacy_upload_unavailable", "message": "This server does not store files in Google Cloud Storage; upload through /upload/multipart (nano-crucible 3.3.0 or newer)"})
    return bucket


def _determine_storage_path(dataset_mfid, filename):
    """
    For a given dataset ID, list the gcs blobs in the corresponding
    folder and return the storage path for the file. 
    """
    bucket = upload_bucket()
    # Files without a recorded location predate the multipart uploads and exist only in Google Cloud Storage.
    if get_settings().storage.backend != "gcs":
        raise HTTPException(status_code=404, detail="File not found in storage")
    basename = filename.split("/")[-1]
    exact = f"{dataset_mfid}/{filename}"
    names = [
        blob.name
        for blob in server_client().bucket(bucket).list_blobs(prefix=f'{dataset_mfid}/', timeout=30)
        if not blob.name.startswith(multipart_prefix(dataset_mfid))
    ]
    if exact in names:
        return f"{bucket}/{exact}"
    candidates = [name for name in names if name.endswith(f"/{basename}")]
    if len(candidates) == 1:
        return f"{bucket}/{candidates[0]}"
    if candidates:
        raise HTTPException(
            status_code=409,
            detail=f"Several stored objects match {basename!r}; the file's location cannot be determined",
        )
    raise HTTPException(status_code=404, detail="File not found in production storage")

def _ensure_storage_path(af, dsid: str, session) -> str:
    """Backfill storage_path from GCS if not set. Returns the storage_path."""
    if not af.storage_path:
        af.storage_path = _determine_storage_path(dsid, af.filename)
        session.add(af)
    return af.storage_path


def _signed_download(file_mfid, dataset_mfid, storage_path, generation, filename, disposition="attachment"):
    """Return (signed_url, expires_in_seconds) for a file record's object, enforcing the storage location rule."""
    try:
        obj = resolve_storage_object(storage_path, generation, dataset_mfid)
    except StorageLocationError as error:
        logger.warning("file_location_rejected file=%s dataset=%s reason=%s", file_mfid, dataset_mfid, error.reason)
        raise HTTPException(status_code=409, detail=error.detail()) from None
    try:
        url = get_store().signed_download(obj.bucket, obj.key, obj.version, filename, disposition, DOWNLOAD_LIFETIME)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found in storage") from None
    except Exception:
        logger.exception("signed_download_failed file=%s dataset=%s", file_mfid, dataset_mfid)
        raise HTTPException(status_code=503, detail="Could not generate a download link; try again later") from None
    return url, int(DOWNLOAD_LIFETIME.total_seconds())



def _mint_upload_token(dsid: str) -> tuple[str, str]:
    """Return (access_token, token_expiry_iso) limited to objects under {dsid}/ in the upload bucket."""
    bucket = upload_bucket()
    from google.auth import downscoped
    from google.auth.transport.requests import Request
    scoped = storage_scoped(server_credentials())
    if not get_settings().storage.legacy_upload_token_downscope:
        scoped.refresh(Request())
        return scoped.token, scoped.expiry.isoformat()
    prefix = f"projects/_/buckets/{bucket}/objects/{dsid}/"
    boundary = downscoped.CredentialAccessBoundary(rules=[downscoped.AccessBoundaryRule(
        available_resource=f"//storage.googleapis.com/projects/_/buckets/{bucket}",
        available_permissions=["inRole:roles/storage.objectAdmin"],
        availability_condition=downscoped.AvailabilityCondition(
            expression=f"resource.name.startsWith('{prefix}') && !resource.name.startsWith('{prefix}{MULTIPART_SEGMENT}/')",
        ),
    )])
    narrowed = downscoped.Credentials(source_credentials=scoped, credential_access_boundary=boundary)
    narrowed.refresh(Request())
    return narrowed.token, narrowed.expiry.isoformat()


def _validate_and_prepare_upload(
    dsid: str,
    body: "UploadInitiate",
    user_unique_id: str,
    principal,
) -> "AssociatedFile | tuple[str, str]":
    """Common preamble for both upload initiation routes.
    Validates size, checks write access, dedup-checks by SHA256, and sanitizes filename.
    Returns AssociatedFile on dedup hit, or (gcs_path, safe_filename) on success.
    """
    if body.size <= 0:
        raise HTTPException(status_code=400, detail="File size must be greater than 0")
    if body.size > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File size {body.size} exceeds the maximum allowed upload size of {_MAX_UPLOAD_BYTES} bytes",
        )

    with Session(get_engine()) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, principal, dsid, Permission.CREATE)
        assert_not_pending_deletion(session, dsid)

        from pathlib import PurePosixPath
        parts = [p for p in PurePosixPath(body.filename).parts if p not in ("", "/", ".", "..")]
        if not parts:
            raise HTTPException(status_code=400, detail="Invalid filename")
        if parts[0] == MULTIPART_SEGMENT:
            raise HTTPException(status_code=400, detail=f"Filenames starting with {MULTIPART_SEGMENT}/ are reserved")
        safe_filename = str(PurePosixPath(*parts))
        if body.sha256_hash:
            existing = session.exec(
                select(AssociatedFile).where(
                    AssociatedFile.dataset_id == ds.id,
                    AssociatedFile.filename == safe_filename,
                    AssociatedFile.sha256_hash == body.sha256_hash,
                    multipart.trusted_hash(),
                )
            ).first()
            if existing is not None:
                return existing
        if multipart.filename_reserved(session, dsid, safe_filename):
            raise HTTPException(status_code=409, detail="Filename is reserved by an active multipart upload")
    return f"{dsid}/{safe_filename}", safe_filename


def _create_upload_session(
    dsid: str,
    gcs_path: str,
    safe_filename: str,
    principal,
    size: int,
) -> str:
    """Persist an UploadSession record and return the generated upload_id."""
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)
    us = UploadSession(
        dataset_mfid=dsid,
        object_key=gcs_path,
        associated_file_name=safe_filename,
        owner_user_id=principal.user_id,
        user_id=principal.internal_user_id,
        expires_at=expires_at,
        size=size,
    )
    with Session(get_engine()) as session:
        session.add(us)
        session.commit()
        session.refresh(us)
    return us.upload_id



@router.post("/datasets/{dataset_mfid}/upload/initiate", deprecated=True, response_model=UploadInitiateRead, responses=LEGACY_UPLOAD_RESPONSE)
def initiate_resumable_upload(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    response: Response,
    body: UploadInitiate,
):
    '''
    Initiate a GCS resumable upload session for a large file.
    Requires contributor access to the dataset.
    Returns an upload_id and a resumable_uri the client uses to PUT chunks directly to GCS
    (no API credentials needed for the chunk transfers).
    Call POST /datasets/{dataset_mfid}/upload/complete when all chunks are uploaded.
    Deprecated: use POST /datasets/{dataset_mfid}/upload/multipart/initiate.
    '''
    response.headers["Deprecation"] = LEGACY_UPLOAD_DEPRECATION
    user_unique_id = auth_res.user_id
    bucket = _legacy_upload_bucket()

    result = _validate_and_prepare_upload(dsid, body, user_unique_id, auth_res)
    if not isinstance(result, tuple):
        return UploadInitiateRead(existing_file=AssociatedFileRead.model_validate(result, update={"dataset_mfid": dsid}))
    gcs_path, safe_filename = result

    try:
        authed = _get_gcs_authed_session()
        resp = authed.post(
            f"{_GCS_UPLOAD_API}/{bucket}/o?uploadType=resumable",
            headers={
                "X-Upload-Content-Type": "application/octet-stream",
                "X-Upload-Content-Length": str(body.size),
            },
            json={"name": gcs_path},
        )
        resp.raise_for_status()
    except Exception:
        logger.exception("resumable_upload_initiate_failed dataset=%s", dsid)
        raise HTTPException(status_code=503, detail="Could not start the upload; try again later") from None

    resumable_uri = resp.headers.get("Location")
    if not resumable_uri:
        raise HTTPException(status_code=503, detail="GCS did not return a resumable upload URI")

    upload_id = _create_upload_session(dsid, gcs_path, safe_filename, auth_res, body.size)

    return UploadInitiateRead(
        upload_id=upload_id,
        resumable_uri=resumable_uri,
        chunk_size_hint=_CHUNK_SIZE_HINT,
    )


@router.post("/datasets/{dataset_mfid}/upload/initiate/multipart", deprecated=True, response_model=UploadMultipartInitiateRead, responses=LEGACY_UPLOAD_RESPONSE)
def initiate_multipart_upload(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    response: Response,
    body: UploadInitiate,
):
    '''
    Vend a short-lived GCS OAuth2 access token for parallel client-side uploads.
    Requires contributor access to the dataset.
    The client uses this token with google-cloud-storage transfer_manager.upload_chunks_concurrently().
    Returns upload_id — pass it to POST /datasets/{dataset_mfid}/upload/complete after the transfer.
    If existing_file is set, the file already exists; skip the upload entirely.
    Deprecated: use POST /datasets/{dataset_mfid}/upload/multipart/initiate.
    '''
    response.headers["Deprecation"] = LEGACY_UPLOAD_DEPRECATION
    user_unique_id = auth_res.user_id
    bucket = _legacy_upload_bucket()

    result = _validate_and_prepare_upload(dsid, body, user_unique_id, auth_res)
    if not isinstance(result, tuple):
        return UploadMultipartInitiateRead(existing_file=AssociatedFileRead.model_validate(result, update={"dataset_mfid": dsid}))
    gcs_path, safe_filename = result

    try:
        access_token, token_expiry = _mint_upload_token(dsid)
    except Exception:
        logger.exception("upload_token_failed dataset=%s", dsid)
        raise HTTPException(status_code=503, detail="Could not start the upload; try again later") from None

    upload_id = _create_upload_session(dsid, gcs_path, safe_filename, auth_res, body.size)

    return UploadMultipartInitiateRead(
        upload_id=upload_id,
        access_token=access_token,
        token_expiry=token_expiry,
        bucket=bucket,
        object_prefix=f"{dsid}/",
        project=get_settings().storage.gcs_project,
    )


@router.post("/datasets/{dataset_mfid}/upload/complete", deprecated=True, response_model=AssociatedFileRead, responses=LEGACY_UPLOAD_RESPONSE)
def complete_resumable_upload(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    response: Response,
    body: UploadComplete,
):
    '''
    Finalise a GCS resumable upload.
    Requires contributor access to the dataset and ownership of the upload session.
    Verifies the blob landed in GCS, registers it as an AssociatedFile on the dataset,
    and deletes the upload session record.
    Deprecated: use POST /datasets/{dataset_mfid}/upload/multipart/initiate.
    '''
    response.headers["Deprecation"] = LEGACY_UPLOAD_DEPRECATION
    bucket = _legacy_upload_bucket()

    with Session(get_engine()) as session:
        us = session.exec(
            select(UploadSession).where(UploadSession.upload_id == body.upload_id)
        ).one_or_none()
        if us is None:
            raise HTTPException(status_code=404, detail="Upload session not found or already completed")
        if not auth_res.has_user_id(us.user_id):
            raise HTTPException(status_code=403, detail="Not the owner of this upload session")
        if us.protocol is not None:
            raise HTTPException(status_code=409, detail="Use the multipart completion route for this upload session")
        if multipart.utc(us.expires_at) < datetime.now(timezone.utc):
            session.delete(us)
            session.commit()
            raise HTTPException(status_code=410, detail="Upload session has expired")
        if us.dataset_mfid != dsid:
            raise HTTPException(status_code=400, detail="Upload session belongs to a different dataset")

        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, auth_res, dsid, Permission.CREATE)
        assert_not_pending_deletion(session, dsid)
        if multipart.filename_reserved(session, dsid, us.associated_file_name):
            raise HTTPException(status_code=409, detail="Filename is reserved by an active multipart upload")

        try:
            blob = server_client().bucket(bucket).blob(us.object_key)
            blob.reload()
        except NotFound:
            raise HTTPException(status_code=422, detail="Uploaded file not found in storage") from None
        except Exception:
            logger.exception("upload_verify_failed dataset=%s upload=%s", dsid, us.upload_id)
            raise HTTPException(status_code=503, detail="Could not verify the upload; try again later") from None

        if us.size is not None and blob.size != us.size:
            raise HTTPException(
                status_code=422,
                detail=f"Upload size mismatch: declared {us.size} bytes, GCS reports {blob.size} bytes",
            )

        assoc = AssociatedFile(
            dataset_id=ds.id,
            filename=us.associated_file_name,
            storage_path=f"{bucket}/{us.object_key}",
            size=blob.size,
            sha256_hash=body.sha256_hash,
        )
        session.add(assoc)
        session.delete(us)
        touch_resource_modification_time(session, dsid)
        session.commit()
        session.refresh(assoc)
        return AssociatedFileRead.model_validate(assoc, update={"dataset_mfid": assoc.dataset.unique_id})
