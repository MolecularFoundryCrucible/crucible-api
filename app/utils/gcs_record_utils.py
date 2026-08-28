import hashlib
import json
import logging

from app.general_utils import get_utc_isoformat

logger = logging.getLogger(__name__)

_RECORD_BUCKET = "mf-record-prod"
_SCHEMA_VERSION = 1


def _get_client():
    from app.api.routes.gcs_storage import _get_gcs_storage_client
    return _get_gcs_storage_client()


def _canonical_hash(payload: dict) -> str:
    """SHA-256 of payload excluding _audit, over canonical JSON (sorted keys)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _build_audit(blob, payload: dict, actor: str, operation: str) -> dict | None:
    """
    Read previous revision from GCS, check if payload changed, build _audit block.
    Returns None if payload is unchanged (skip write).
    """
    revision = 1
    prev_hash = None

    try:
        if blob.exists():
            prev = json.loads(blob.download_as_text())
            prev_audit = prev.get("_audit", {})
            revision = prev_audit.get("revision", 0) + 1
            prev_hash = prev_audit.get("content_sha256")
    except Exception:
        logger.warning("Could not read previous revision for %s — starting at 1", blob.name)

    content_sha256 = _canonical_hash(payload)
    if prev_hash == content_sha256:
        logger.info("No change in %s — skipping write", blob.name)
        return None

    return {
        "revision": revision,
        "operation": operation,
        "actor": actor,
        "timestamp": get_utc_isoformat(),
        "content_sha256": content_sha256,
        "_schema_version": _SCHEMA_VERSION,
    }


def dataset_record(ds, ri) -> dict:
    return {
        "unique_id": ds.unique_id, "dataset_name": ds.dataset_name,
        "owner_orcid": ds.owner_orcid, "project_id": ds.project_id,
        "instrument_name": ds.instrument_name, "measurement": ds.measurement,
        "data_type": ds.data_type, "data_format": ds.data_format,
        "session_name": ds.session_name,
        "timestamp": ds.timestamp, "size": ds.size,
        "public": ri.public if ri else ds.public,
        "resource_type": "dataset",
        "creation_time": ri.creation_time if ri else None,
        "modification_time": ri.modification_time if ri else None,
    }


def sample_record(samp, ri) -> dict:
    return {
        "unique_id": samp.unique_id, "sample_name": samp.sample_name,
        "sample_type": samp.sample_type, "owner_orcid": samp.owner_orcid,
        "project_id": samp.project_id, "description": samp.description,
        "timestamp": samp.timestamp,
        "public": ri.public if ri else False,
        "resource_type": "sample",
        "creation_time": ri.creation_time if ri else None,
        "modification_time": ri.modification_time if ri else None,
    }


def instrument_record(inst, ri) -> dict:
    return {
        "unique_id": inst.unique_id, "instrument_name": inst.instrument_name,
        "manufacturer": inst.manufacturer, "model": inst.model,
        "owner": inst.owner, "other_id": inst.other_id,
        "other_id_source": inst.other_id_source,
        "resource_type": "instrument",
        "creation_time": ri.creation_time if ri else None,
        "modification_time": ri.modification_time if ri else None,
    }


def write_record(mfid: str, record: dict, actor: str, operation: str) -> None:
    """Upload record.json for a resource. Best-effort — logs and returns on failure."""
    try:
        blob = _get_client().bucket(_RECORD_BUCKET).blob(f"{mfid}/record.json")
        audit = _build_audit(blob, record, actor, operation)
        if audit is None:
            return
        blob.upload_from_string(
            json.dumps({"_audit": audit, **record}, default=str, indent=2),
            content_type="application/json",
        )
        logger.info("Wrote record.json for %s (revision %d)", mfid, audit["revision"])
    except Exception:
        logger.exception("Failed to write record.json for %s", mfid)


def write_metadata(mfid: str, metadata: dict, actor: str, operation: str) -> None:
    """Upload metadata.json for a resource. Best-effort — logs and returns on failure."""
    try:
        blob = _get_client().bucket(_RECORD_BUCKET).blob(f"{mfid}/metadata.json")
        audit = _build_audit(blob, metadata, actor, operation)
        if audit is None:
            return
        blob.upload_from_string(
            json.dumps({"_audit": audit, **metadata}, default=str, indent=2),
            content_type="application/json",
        )
        logger.info("Wrote metadata.json for %s (revision %d)", mfid, audit["revision"])
    except Exception:
        logger.exception("Failed to write metadata.json for %s", mfid)


