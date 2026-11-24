import logging

from fastapi import HTTPException

from app.config import get_settings
from app.services.resource_settings import read_settings
from app.services.storage_downloads import StorageNotConfigured

logger = logging.getLogger(__name__)
_reported: set[str] = set()


def unknown_store(store_id: str) -> HTTPException:
    return HTTPException(422, {"code": "unknown_store", "store_id": store_id, "message": f"{store_id} is not a store on this server (see GET /storage/stores)"})


def choose_store(session, dataset, requested: str | None):
    """The store a new upload into the dataset goes to: the one requested, else the project's default,
    else the instrument's, else the deployment's. A default naming a store this server no longer has
    is skipped; a request naming one is refused."""
    storage = get_settings().storage
    if requested is not None:
        store = storage.store(requested)
        if store is None:
            raise unknown_store(requested)
        return store
    for mfid in (dataset.project_mfid, dataset.instrument_mfid):
        wanted = read_settings(session, mfid).get("default_store_id") if mfid else None
        if wanted is None:
            continue
        store = storage.store(wanted)
        if store is not None:
            return store
        if wanted not in _reported:
            _reported.add(wanted)
            logger.warning("default_store_unavailable store=%s record=%s", wanted, mfid)
    store = storage.store(storage.default_store_id)
    if store is None:
        raise StorageNotConfigured("File storage is not configured on this server (STORAGE_STORES is not set)")
    return store
