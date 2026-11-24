from functools import lru_cache

from app.storage.base import ObjectInfo, ObjectStore, Part, StorageFailure, StoreUnavailable, UploadMissing


def get_store(store_id: str | None) -> ObjectStore:
    """The adapter for the store a row names. Raises StoreUnavailable for an id this server does not
    configure, and for a row with no store recorded."""
    if store_id is None:
        raise StoreUnavailable(None)
    return _store(store_id)


def get_default_store() -> ObjectStore:
    """The adapter for STORAGE_DEFAULT_STORE (or the only store)."""
    from app.config import get_settings
    from app.services.storage_downloads import StorageNotConfigured

    default = get_settings().storage.default_store_id
    if default is None:
        raise StorageNotConfigured("File storage is not configured on this server (STORAGE_STORES is not set)")
    return _store(default)


@lru_cache
def _store(store_id: str) -> ObjectStore:
    from app.config import get_settings

    store = get_settings().storage.store(store_id)
    if store is None:
        raise StoreUnavailable(store_id)
    if store.type == "s3":
        from app.storage.s3 import S3Store

        return S3Store(store)
    from app.storage.gcs import GoogleStore
    from app.utils.multipart_config import MultipartStorageConfig

    return GoogleStore(MultipartStorageConfig.from_env(), store)


get_store.cache_clear = _store.cache_clear


def store_config(store_id: str | None):
    """The configuration of the store a row names; StoreUnavailable when this server lacks it or the
    row names none, StorageNotConfigured when it configures no store at all."""
    from app.config import get_settings
    from app.services.storage_downloads import StorageNotConfigured

    storage = get_settings().storage
    if not storage.stores:
        raise StorageNotConfigured("File storage is not configured on this server (STORAGE_STORES is not set)")
    store = storage.store(store_id)
    if store is None:
        raise StoreUnavailable(store_id)
    return store


__all__ = ["ObjectInfo", "ObjectStore", "Part", "StorageFailure", "StoreUnavailable", "UploadMissing", "get_default_store", "get_store", "store_config"]
