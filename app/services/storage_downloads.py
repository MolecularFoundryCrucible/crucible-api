import mimetypes
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import PurePosixPath
from urllib.parse import quote

from google.api_core.exceptions import NotFound

LEGACY_BUCKET = "mf-storage-prod"
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
    generation: int | None


def multipart_prefix(dataset_mfid):
    return f"{dataset_mfid}/{MULTIPART_SEGMENT}/"


def allowed_buckets():
    return {LEGACY_BUCKET, os.environ.get("MULTIPART_UPLOAD_BUCKET", "").strip()} - {""}


def resolve_storage_object(storage_path, generation, dataset_mfid):
    bucket, separator, key = (storage_path or "").partition("/")
    if not separator or not bucket or not key:
        raise StorageLocationError("unparseable")
    if bucket not in allowed_buckets():
        raise StorageLocationError("bucket_not_allowed")
    segments = key.split("/")
    if key.startswith("/") or any(segment in (".", "..") for segment in segments):
        raise StorageLocationError("invalid_segments")
    if segments[0] != dataset_mfid or not any(segments[1:]):
        raise StorageLocationError("outside_dataset")
    if key.startswith(multipart_prefix(dataset_mfid)) and generation is None:
        raise StorageLocationError("unpublished")
    return StorageObject(bucket, key, int(generation) if generation is not None else None)


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


def signed_download_url(client, obj, filename, disposition="attachment"):
    blob = client.bucket(obj.bucket).blob(obj.key, generation=obj.generation)
    try:
        blob.reload()
    except NotFound:
        raise FileNotFoundError(obj.key) from None
    content_disposition, content_type = download_headers(filename, blob.content_type, disposition)
    overrides = {"response_disposition": content_disposition}
    if content_type:
        overrides["response_type"] = content_type
    return blob.generate_signed_url(version="v4", expiration=DOWNLOAD_LIFETIME, method="GET", generation=obj.generation, **overrides)
