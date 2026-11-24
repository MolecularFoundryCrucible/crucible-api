import mimetypes
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import quote

MULTIPART_SEGMENT = "_files"
DOWNLOAD_LIFETIME = timedelta(hours=1)
SCRIPT_CAPABLE_TYPES = frozenset({"text/html", "application/xhtml+xml", "image/svg+xml", "text/xml", "application/xml", "text/xsl"})


class StorageLocationError(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason

    def detail(self):
        return {"code": "storage_location_invalid", "reason": self.reason, "message": f"File storage location is not valid: {self.reason}"}


@dataclass(frozen=True)
class StorageObject:
    bucket: str
    key: str
    version: str | None


def multipart_prefix(dataset_mfid):
    return f"{dataset_mfid}/{MULTIPART_SEGMENT}/"


class StorageNotConfigured(Exception):
    pass



def store_buckets(store):
    """The buckets a store's files may be in: its own, and for the default store STORAGE_READ_BUCKETS."""
    from app.config import get_settings

    settings = get_settings().storage
    buckets = {store.bucket} - {"", None}
    if store.id == settings.default_store_id:
        buckets |= settings.read_bucket_names
    return buckets


def resolve_storage_object(store, storage_path, generation, dataset_mfid):
    allowed = store_buckets(store)
    if not allowed:
        raise StorageNotConfigured("File storage is not configured on this server (UPLOAD_BUCKET is not set)")
    bucket, separator, key = (storage_path or "").partition("/")
    if not separator or not bucket or not key:
        raise StorageLocationError("unparseable")
    if bucket not in allowed:
        raise StorageLocationError("bucket_not_allowed")
    segments = key.split("/")
    if key.startswith("/") or any(segment in (".", "..") for segment in segments):
        raise StorageLocationError("invalid_segments")
    if segments[0] != dataset_mfid or not any(segments[1:]):
        raise StorageLocationError("outside_dataset")
    if key.startswith(multipart_prefix(dataset_mfid)) and generation is None:
        raise StorageLocationError("unpublished")
    return StorageObject(bucket, key, str(generation) if generation is not None else None)


def owned_object(store, storage_path, generation, dataset_mfid):
    """Whether the API may delete this object: it is in its store's own bucket and inside its
    dataset, by the same rule downloads follow."""
    bucket = (storage_path or "").partition("/")[0]
    if not store.bucket or bucket != store.bucket:
        return False
    try:
        resolve_storage_object(store, storage_path, generation, dataset_mfid)
    except (StorageLocationError, StorageNotConfigured):
        return False
    return True


def download_headers(filename, stored_type, disposition="attachment"):
    basename = PurePosixPath(filename or "").name
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in basename).strip() or "download"
    encoded = quote(basename, safe="") or "download"
    if disposition != "inline":
        return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{encoded}", None
    guessed = mimetypes.guess_type(basename)[0] if basename else None
    effective = (guessed or stored_type or "").split(";")[0].strip().lower()
    mode = "attachment" if effective in SCRIPT_CAPABLE_TYPES or effective.endswith("+xml") else "inline"
    return f"{mode}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}", guessed

