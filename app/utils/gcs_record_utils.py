import json
import logging

logger = logging.getLogger(__name__)

_RECORD_BUCKET = "mf-record-prod"


def _get_client():
    from app.api.routes.gcs_storage import _get_gcs_storage_client
    return _get_gcs_storage_client()


def write_record(mfid: str, record: dict) -> None:
    """Upload record.json for a resource. Best-effort — logs and returns on failure."""
    try:
        blob = _get_client().bucket(_RECORD_BUCKET).blob(f"{mfid}/record.json")
        blob.upload_from_string(
            json.dumps(record, default=str, indent=2),
            content_type="application/json",
        )
        logger.info("Wrote record.json for %s", mfid)
    except Exception:
        logger.exception("Failed to write record.json for %s", mfid)


def write_metadata(mfid: str, metadata: dict) -> None:
    """Upload metadata.json for a resource. Best-effort — logs and returns on failure."""
    try:
        blob = _get_client().bucket(_RECORD_BUCKET).blob(f"{mfid}/metadata.json")
        blob.upload_from_string(
            json.dumps(metadata, default=str, indent=2),
            content_type="application/json",
        )
        logger.info("Wrote metadata.json for %s", mfid)
    except Exception:
        logger.exception("Failed to write metadata.json for %s", mfid)
