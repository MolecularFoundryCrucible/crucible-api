import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import DatasetMfid
from app.general_utils import get_secret
from app.models.core_models import AssociatedFile, Dataset, UploadSession
from app.models.datasets import AssociatedFileRead
from app.models.uploads import UploadComplete, UploadInitiate, UploadInitiateRead, UploadMultipartInitiateRead
from app.services import multipart
from app.utils.auth_utils import authorize_user, permission_filter, require_permission
from app.utils.permissions import Principal, Permission
from app.utils.sql_utils import add_or_update

router = APIRouter(tags=["storage"])
engine = get_engine()

_DOWNLOAD_BUCKET = "mf-storage-prod"
_GCS_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1/b"
_CHUNK_SIZE_HINT = 8 * 1024 * 1024  # 8 MiB — GCS recommended chunk multiple
_MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 500 * 1024 ** 3))  # default 500 GiB
_GCS_PROJECT = os.environ.get("GCS_PROJECT", "crucible-prod")

_gcs_credentials = None
_gcs_authed_session = None
_gcs_storage_client = None


def _get_gcs_credentials():
    global _gcs_credentials
    if _gcs_credentials is None:
        from google.oauth2 import service_account
        sa_creds = get_secret("GCS_SA", "service-account-cred/versions/latest")
        service_account_info = json.loads(sa_creds)
        _gcs_credentials = service_account.Credentials.from_service_account_info(service_account_info)
    return _gcs_credentials


def _get_gcs_authed_session():
    global _gcs_authed_session
    if _gcs_authed_session is None:
        from google.auth.transport.requests import AuthorizedSession
        scoped = _get_gcs_credentials().with_scopes(
            ["https://www.googleapis.com/auth/devstorage.read_write"]
        )
        _gcs_authed_session = AuthorizedSession(scoped)
    return _gcs_authed_session


def _get_gcs_storage_client():
    global _gcs_storage_client
    if _gcs_storage_client is None:
        from google.cloud import storage as gcs
        _gcs_storage_client = gcs.Client(credentials=_get_gcs_credentials())
    return _gcs_storage_client


def _determine_storage_path(dataset_mfid, filename):
    """
    For a given dataset ID, list the gcs blobs in the corresponding
    folder and return the storage path for the file. 
    """
    basename = filename.split("/")[-1]
    ds_blobs = _get_gcs_storage_client().bucket(_DOWNLOAD_BUCKET).list_blobs(prefix=f'{dataset_mfid}/')
    for blob in ds_blobs:
        if blob.name.endswith(basename):
            return f"{_DOWNLOAD_BUCKET}/{blob.name}"
    else:
        raise HTTPException(
            status_code=404,
            detail="File not found in production storage",
        )

def _ensure_storage_path(af, dsid: str, session) -> str:
    """Backfill storage_path from GCS if not set. Returns the storage_path."""
    if not af.storage_path:
        af.storage_path = _determine_storage_path(dsid, af.filename)
        session.add(af)
    return af.storage_path


def _generate_signed_url_link(cloud_storage_path, expire_hours=1, generation=None, filename=None):
    """Return (signed_url, expires_in_seconds), or raise 404 if the blob does not exist."""
    bucket_name, _, blob_name = cloud_storage_path.partition("/")
    from app.utils.multipart_config import MultipartStorageConfig
    config = MultipartStorageConfig.from_env()
    if bucket_name == config.bucket:
        if generation is None:
            raise HTTPException(403, "Multipart storage requires a registered generation")
        from app.services.multipart_storage import get_multipart_storage
        try:
            url = get_multipart_storage().download(bucket_name, blob_name, generation, datetime.now(timezone.utc) + timedelta(hours=expire_hours), filename)
        except FileNotFoundError:
            raise HTTPException(404, "File not found in storage") from None
        except Exception:
            raise HTTPException(503, "Download signing unavailable") from None
        return url, expire_hours * 60 * 60
    blob = _get_gcs_storage_client().bucket(bucket_name).blob(blob_name)

    if not blob.exists():
        raise HTTPException(
            status_code=404,
            detail=f"File not found in storage: {cloud_storage_path}",
        )

    try:
        url = blob.generate_signed_url(version="v4", expiration=timedelta(hours=expire_hours), method="GET")
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not generate signed URL: {e}")

    return url, expire_hours * 60 * 60



def _mint_upload_token() -> tuple[str, str]:
    """Return (access_token, token_expiry_iso) by refreshing the service account credentials."""
    from google.auth.transport.requests import Request
    scoped = _get_gcs_credentials().with_scopes(
        ["https://www.googleapis.com/auth/devstorage.read_write"]
    )
    scoped.refresh(Request())
    return scoped.token, scoped.expiry.isoformat()


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

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, principal, dsid, Permission.CREATE)

        if body.sha256_hash:
            existing = session.exec(
                select(AssociatedFile).where(
                    AssociatedFile.dataset_id == ds.id,
                    AssociatedFile.sha256_hash == body.sha256_hash,
                    multipart.trusted_hash(),
                )
            ).first()
            if existing is not None:
                return existing # TODO - this feels wrong, can return relative storage path, filename

        from pathlib import PurePosixPath
        parts = [p for p in PurePosixPath(body.filename).parts if p not in ("", "/", ".", "..")]
        if not parts:
            raise HTTPException(status_code=400, detail="Invalid filename")
        safe_filename = str(PurePosixPath(*parts))
        if multipart.filename_reserved(session, dsid, safe_filename):
            raise HTTPException(status_code=409, detail="Filename is reserved by an active multipart upload")
    return f"{dsid}/{safe_filename}", safe_filename


def _create_upload_session(
    dsid: str,
    gcs_path: str,
    safe_filename: str,
    user_unique_id: str,
    size: int,
) -> str:
    """Persist an UploadSession record and return the generated upload_id."""
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    us = UploadSession(
        dataset_mfid=dsid,
        gcs_path=gcs_path,
        associated_file_name=safe_filename,
        owner_user_id=user_unique_id,
        expires_at=expires_at,
        size=size,
    )
    with Session(engine) as session:
        session.add(us)
        session.commit()
        session.refresh(us)
    return us.upload_id



@router.post("/datasets/{dataset_mfid}/upload/initiate", response_model=UploadInitiateRead)
def initiate_resumable_upload(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    body: UploadInitiate,
):
    '''
    Initiate a GCS resumable upload session for a large file.
    Requires contributor access to the dataset.
    Returns an upload_id and a resumable_uri the client uses to PUT chunks directly to GCS
    (no API credentials needed for the chunk transfers).
    Call POST /datasets/{dataset_mfid}/upload/complete when all chunks are uploaded.
    '''
    user_unique_id = auth_res.user_id

    result = _validate_and_prepare_upload(dsid, body, user_unique_id, auth_res)
    if not isinstance(result, tuple):
        return UploadInitiateRead(existing_file=AssociatedFileRead.model_validate(result, update={"dataset_mfid": dsid}))
    gcs_path, safe_filename = result

    try:
        authed = _get_gcs_authed_session()
        resp = authed.post(
            f"{_GCS_UPLOAD_API}/{_DOWNLOAD_BUCKET}/o?uploadType=resumable",
            headers={
                "X-Upload-Content-Type": "application/octet-stream",
                "X-Upload-Content-Length": str(body.size),
            },
            json={"name": gcs_path},
        )
        resp.raise_for_status()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not initiate GCS resumable upload: {e}")

    resumable_uri = resp.headers.get("Location")
    if not resumable_uri:
        raise HTTPException(status_code=503, detail="GCS did not return a resumable upload URI")

    upload_id = _create_upload_session(dsid, gcs_path, safe_filename, user_unique_id, body.size)

    return UploadInitiateRead(
        upload_id=upload_id,
        resumable_uri=resumable_uri,
        chunk_size_hint=_CHUNK_SIZE_HINT,
    )


@router.post("/datasets/{dataset_mfid}/upload/initiate/multipart", response_model=UploadMultipartInitiateRead)
def initiate_multipart_upload(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    body: UploadInitiate,
):
    '''
    Vend a short-lived GCS OAuth2 access token for parallel client-side uploads.
    Requires contributor access to the dataset.
    The client uses this token with google-cloud-storage transfer_manager.upload_chunks_concurrently().
    Returns upload_id — pass it to POST /datasets/{dataset_mfid}/upload/complete after the transfer.
    If existing_file is set, the file already exists; skip the upload entirely.
    '''
    user_unique_id = auth_res.user_id

    result = _validate_and_prepare_upload(dsid, body, user_unique_id, auth_res)
    if not isinstance(result, tuple):
        return UploadMultipartInitiateRead(existing_file=AssociatedFileRead.model_validate(result, update={"dataset_mfid": dsid}))
    gcs_path, safe_filename = result

    try:
        access_token, token_expiry = _mint_upload_token()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not generate GCS upload token: {e}")

    upload_id = _create_upload_session(dsid, gcs_path, safe_filename, user_unique_id, body.size)

    return UploadMultipartInitiateRead(
        upload_id=upload_id,
        access_token=access_token,
        token_expiry=token_expiry,
        bucket=_DOWNLOAD_BUCKET,
        object_prefix=f"{dsid}/",
        project=_GCS_PROJECT,
    )


@router.post("/datasets/{dataset_mfid}/upload/complete", response_model=AssociatedFileRead)
def complete_resumable_upload(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    dsid: DatasetMfid,
    body: UploadComplete,
):
    '''
    Finalise a GCS resumable upload.
    Requires contributor access to the dataset and ownership of the upload session.
    Verifies the blob landed in GCS, registers it as an AssociatedFile on the dataset,
    and deletes the upload session record.
    '''
    user_unique_id = auth_res.user_id

    with Session(engine) as session:
        us = session.exec(
            select(UploadSession).where(UploadSession.upload_id == body.upload_id)
        ).one_or_none()
        if us is None:
            raise HTTPException(status_code=404, detail="Upload session not found or already completed")
        if us.owner_user_id != user_unique_id:
            raise HTTPException(status_code=403, detail="Not the owner of this upload session")
        if us.protocol is not None:
            raise HTTPException(status_code=409, detail="Use the multipart completion route for this upload session")
        expires_at = us.expires_at if isinstance(us.expires_at, datetime) else datetime.fromisoformat(us.expires_at)
        if expires_at < datetime.now(timezone.utc):
            session.delete(us)
            session.commit()
            raise HTTPException(status_code=410, detail="Upload session has expired")
        if us.dataset_mfid != dsid:
            raise HTTPException(status_code=400, detail="Upload session belongs to a different dataset")

        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_permission(session, auth_res, dsid, Permission.CREATE)
        if multipart.filename_reserved(session, dsid, us.associated_file_name):
            raise HTTPException(status_code=409, detail="Filename is reserved by an active multipart upload")

        try:
            blob = _get_gcs_storage_client().bucket(_DOWNLOAD_BUCKET).blob(us.gcs_path)
            blob.reload()
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"Could not verify upload in GCS: {e}")

        if us.size is not None and blob.size != us.size:
            raise HTTPException(
                status_code=422,
                detail=f"Upload size mismatch: declared {us.size} bytes, GCS reports {blob.size} bytes",
            )

        assoc = AssociatedFile(
            dataset_id=ds.id,
            filename=us.associated_file_name,
            storage_path=f"{_DOWNLOAD_BUCKET}/{us.gcs_path}",
            size=blob.size,
            sha256_hash=body.sha256_hash,
        )
        session.add(assoc)
        session.delete(us)
        session.commit()
        session.refresh(assoc)
        return AssociatedFileRead.model_validate(assoc, update={"dataset_mfid": assoc.dataset.unique_id})
