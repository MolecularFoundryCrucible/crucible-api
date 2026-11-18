import logging

from fastapi import APIRouter, HTTPException

from app.config import get_settings
from app.services.storage_downloads import DOWNLOAD_LIFETIME, StorageLocationError, multipart_prefix, resolve_storage_object, upload_bucket
from app.storage import get_store
from app.storage.gcs import server_client

router = APIRouter(tags=["storage"])
logger = logging.getLogger(__name__)

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
