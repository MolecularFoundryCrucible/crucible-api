from functools import lru_cache

from app.storage.base import ObjectInfo, ObjectStore, Part, StorageFailure, UploadMissing


@lru_cache
def get_store() -> ObjectStore:
    from app.storage.gcs import GoogleStore
    from app.utils.multipart_config import MultipartStorageConfig

    return GoogleStore(MultipartStorageConfig.from_env())


__all__ = ["ObjectInfo", "ObjectStore", "Part", "StorageFailure", "UploadMissing", "get_store"]
