from functools import lru_cache

from app.storage.base import ObjectInfo, ObjectStore, Part, StorageFailure, UploadMissing


@lru_cache
def get_store() -> ObjectStore:
    from app.config import get_settings

    storage = get_settings().storage
    if storage.backend == "s3":
        from app.storage.s3 import S3Store

        return S3Store(storage)
    from app.storage.gcs import GoogleStore
    from app.utils.multipart_config import MultipartStorageConfig

    return GoogleStore(MultipartStorageConfig.from_env())


__all__ = ["ObjectInfo", "ObjectStore", "Part", "StorageFailure", "UploadMissing", "get_store"]
