import hashlib
import json
import logging

from app.config import get_settings
from app.general_utils import get_utc_isoformat
from app.storage import get_default_store

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


def _record_bucket():
    bucket = get_settings().storage.record_bucket or ""
    if not bucket:
        logger.warning("RECORD_BUCKET is not set; record files are not written")
    return bucket


def _canonical_hash(payload: dict) -> str:
    """SHA-256 of payload excluding _audit, over canonical JSON (sorted keys)."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()


def _build_audit(store, bucket: str, key: str, payload: dict, actor: str, operation: str) -> dict | None:
    """
    Read previous revision from storage, check if payload changed, build _audit block.
    Returns None if payload is unchanged (skip write).
    """
    revision = 1
    prev_hash = None

    try:
        text = store.get_text(bucket, key)
        if text is not None:
            prev = json.loads(text)
            prev_audit = prev.get("_audit", {})
            revision = prev_audit.get("revision", 0) + 1
            prev_hash = prev_audit.get("content_sha256")
    except Exception:
        logger.warning("Could not read previous revision for %s — starting at 1", key)

    content_sha256 = _canonical_hash(payload)
    if prev_hash == content_sha256:
        logger.info("No change in %s — skipping write", key)
        return None

    return {
        "revision": revision,
        "operation": operation,
        "actor": actor,
        "timestamp": get_utc_isoformat(),
        "content_sha256": content_sha256,
        "_schema_version": _SCHEMA_VERSION,
    }


def dataset_record(ds, ri, public: bool, owner_id: str | None) -> dict:
    return {
        "unique_id": ds.unique_id, "dataset_name": ds.dataset_name,
        "owner_orcid": owner_id, "project_mfid": ds.project_mfid,
        "instrument_mfid": ds.instrument_mfid, "measurement": ds.measurement,
        "data_type": ds.data_type, "data_format": ds.data_format,
        "session_name": ds.session_name,
        "timestamp": ds.timestamp, "size": ds.size,
        "public": public,
        "resource_type": "dataset",
        "creation_time": ri.creation_time if ri else None,
        "modification_time": ri.modification_time if ri else None,
    }


def sample_record(samp, ri, public: bool, owner_id: str | None) -> dict:
    return {
        "unique_id": samp.unique_id, "sample_name": samp.sample_name,
        "sample_type": samp.sample_type, "owner_orcid": owner_id,
        "project_mfid": samp.project_mfid, "description": samp.description,
        "timestamp": samp.timestamp,
        "public": public,
        "resource_type": "sample",
        "creation_time": ri.creation_time if ri else None,
        "modification_time": ri.modification_time if ri else None,
    }


def instrument_record(inst, ri, owner) -> dict:
    from app.utils.user_utils import owner_reference

    return {
        "unique_id": inst.unique_id, "instrument_id": inst.instrument_id,
        "instrument_name": inst.instrument_name,
        "manufacturer": inst.manufacturer, "model": inst.model,
        "owner": owner_reference(inst, owner), "other_id": inst.other_id,
        "other_id_source": inst.other_id_source,
        "location": inst.location, "description": inst.description,
        "instrument_type": inst.instrument_type, "status": inst.status,
        "resource_type": "instrument",
        "creation_time": ri.creation_time if ri else None,
        "modification_time": ri.modification_time if ri else None,
    }


def write_record(mfid: str, record: dict, actor: str, operation: str) -> None:
    """Upload record.json for a resource. Best-effort — logs and returns on failure."""
    bucket = _record_bucket()
    if not bucket:
        return
    try:
        store, key = get_default_store(), f"{mfid}/record.json"
        audit = _build_audit(store, bucket, key, record, actor, operation)
        if audit is None:
            return
        store.put_bytes(bucket, key, json.dumps({"_audit": audit, **record}, default=str, indent=2).encode(), "application/json")
        logger.info("Wrote record.json for %s (revision %d)", mfid, audit["revision"])
    except Exception:
        logger.exception("Failed to write record.json for %s", mfid)


def write_metadata(mfid: str, metadata: dict, actor: str, operation: str) -> None:
    """Upload metadata.json for a resource. Best-effort — logs and returns on failure."""
    bucket = _record_bucket()
    if not bucket:
        return
    try:
        store, key = get_default_store(), f"{mfid}/metadata.json"
        audit = _build_audit(store, bucket, key, metadata, actor, operation)
        if audit is None:
            return
        store.put_bytes(bucket, key, json.dumps({"_audit": audit, **metadata}, default=str, indent=2).encode(), "application/json")
        logger.info("Wrote metadata.json for %s (revision %d)", mfid, audit["revision"])
    except Exception:
        logger.exception("Failed to write metadata.json for %s", mfid)


