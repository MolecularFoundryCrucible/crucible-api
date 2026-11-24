from functools import lru_cache

from app.storage.base import ObjectInfo, ObjectStore, Part, StorageFailure, StoreUnavailable, UploadMissing


def get_store(store_id: str | None = None) -> ObjectStore:
    """The adapter for a store; None asks for the default store. Raises StoreUnavailable for an id
    this server does not configure."""
    from app.config import get_settings

    return _store(store_id or get_settings().storage.default_store_id)


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


__all__ = ["ObjectInfo", "ObjectStore", "Part", "StorageFailure", "StoreUnavailable", "UploadMissing", "get_store"]
