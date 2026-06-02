import json
import logging

logger = logging.getLogger(__name__)

_RECORD_BUCKET = "mf-record-prod"


def _get_client():
    from app.api.routes.gcs_storage import _get_gcs_storage_client
    return _get_gcs_storage_client()


def dataset_record(ds, ri) -> dict:
    return {
        "unique_id": ds.unique_id, "dataset_name": ds.dataset_name,
        "owner_orcid": ds.owner_orcid, "project_id": ds.project_id,
        "instrument_name": ds.instrument_name, "measurement": ds.measurement,
        "data_type": ds.data_type, "data_format": ds.data_format,
        "session_name": ds.session_name, "source_folder": ds.source_folder,
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
