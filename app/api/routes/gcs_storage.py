import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_secret
from app.models.core_models import AssociatedFile, Dataset, UploadSession
from app.models.datasets import AssociatedFileRead
from app.models.uploads import UploadComplete, UploadInitiate, UploadInitiateRead
from app.utils.auth_utils import authorize_user, require_read_access, require_write_access
from app.utils.sql_utils import add_or_update

router = APIRouter(tags=["storage"])
engine = get_engine()

_UPLOAD_BUCKET = "crucible-uploads"
_DOWNLOAD_BUCKET = "mf-storage-prod"
_GCS_UPLOAD_API = "https://storage.googleapis.com/upload/storage/v1/b"
_CHUNK_SIZE_HINT = 8 * 1024 * 1024  # 8 MiB — GCS recommended chunk multiple
_MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", 500 * 1024 ** 3))  # default 500 GiB

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



@router.get('/datasets/{dsid}/download_links')
def generate_signed_url_download_link(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str,
):
    '''
    Returns signed GCS download URLs (1-hour expiry) for all ingested files in the dataset.
    Keys are AssociatedFile MFIDs where a match is found, falling back to the GCS blob path.
    '''
    _, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        require_read_access(session, dsid, ag_ids)

        # Build blob_name → mfid map from AssociatedFile records
        af_records = session.exec(
            select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)
        ).all()

    blob_to_mfid = {}
    for af in af_records:
        if af.storage_path:
            blob_name = af.storage_path.partition("/")[2]
            blob_to_mfid[blob_name] = af.mfid

    try:
        storage_client = _get_gcs_storage_client()
        bucket_obj = storage_client.bucket(_DOWNLOAD_BUCKET)
        ds_blobs = list(bucket_obj.list_blobs(prefix=f'{dsid}/'))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not access storage bucket: {e}")

    if not ds_blobs:
        raise HTTPException(status_code=404, detail=f"No files found for dataset '{dsid}'")

    try:
        def _sign(blob):
            key = blob_to_mfid.get(blob.name, blob.name)
            return key, blob.generate_signed_url(version="v4", expiration=timedelta(hours=1), method="GET")

        with ThreadPoolExecutor() as executor:
            urls = dict(executor.map(_sign, ds_blobs))
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Could not generate signed URLs: {e}")

    return urls


@router.post("/datasets/{dsid}/upload/initiate", response_model=UploadInitiateRead)
def initiate_resumable_upload(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str,
    body: UploadInitiate,
):
    '''
    Initiate a GCS resumable upload session for a large file.
    Returns an upload_id and a resumable_uri the client uses to PUT chunks directly to GCS
    (no API credentials needed for the chunk transfers).
    Call POST /datasets/{dsid}/upload/complete when all chunks are uploaded.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

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
        require_write_access(session, ag_ids, [dsid])

        if body.sha256_hash:
            existing = session.exec(
                select(AssociatedFile).where(
                    AssociatedFile.dataset_id == ds.id,
                    AssociatedFile.sha256_hash == body.sha256_hash,
                )
            ).one_or_none()
            if existing is not None:
                return UploadInitiateRead(existing_file=AssociatedFileRead.model_validate(existing, update={"dataset_mfid": existing.dataset.unique_id}))

    # Strip absolute markers, current-dir dots, and traversal components.
    from pathlib import PurePosixPath
    parts = [p for p in PurePosixPath(body.filename).parts if p not in ("", "/", ".", "..")]
    if not parts:
        raise HTTPException(status_code=400, detail="Invalid filename")
    safe_filename = str(PurePosixPath(*parts))

    gcs_path = f"{dsid}/{safe_filename}"
    associated_file_name = f"{_DOWNLOAD_BUCKET}/{gcs_path}"

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

    upload_id = str(uuid.uuid4())
    expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()

    with Session(engine) as session:
        session.add(UploadSession(
            upload_id=upload_id,
            dataset_mfid=dsid,
            gcs_path=gcs_path,
            associated_file_name=associated_file_name,
            owner_user_id=user_unique_id,
            expires_at=expires_at,
            size=body.size,
        ))
        session.commit()

    return UploadInitiateRead(
        upload_id=upload_id,
        resumable_uri=resumable_uri,
        chunk_size_hint=_CHUNK_SIZE_HINT,
    )


@router.post("/datasets/{dsid}/upload/complete", response_model=AssociatedFileRead)
def complete_resumable_upload(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str,
    body: UploadComplete,
):
    '''
    Finalise a GCS resumable upload.
    Verifies the blob landed in GCS, registers it as an AssociatedFile on the dataset,
    and deletes the upload session record.
    '''
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]

    with Session(engine) as session:
        us = session.exec(
            select(UploadSession).where(UploadSession.upload_id == body.upload_id)
        ).one_or_none()
        if us is None:
            raise HTTPException(status_code=404, detail="Upload session not found or already completed")
        if us.owner_user_id != user_unique_id:
            raise HTTPException(status_code=403, detail="Not the owner of this upload session")
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
        require_write_access(session, ag_ids, [dsid])

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
            storage_path=us.associated_file_name,  # already in prod — set immediately
            size=blob.size,
            sha256_hash=body.sha256_hash,
        )
        session.add(assoc)
        session.delete(us)
        session.commit()
        session.refresh(assoc)
        return AssociatedFileRead.model_validate(assoc, update={"dataset_mfid": assoc.dataset.unique_id})
