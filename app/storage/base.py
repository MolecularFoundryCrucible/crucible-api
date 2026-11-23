from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class StorageFailure(Exception):
    pass


class UploadMissing(StorageFailure):
    pass


@dataclass(frozen=True)
class Part:
    """An uploaded part. `crc32c` is the base64 checksum the store computed, when it reports one."""
    part_number: int
    size: int
    etag: str
    crc32c: str | None = None


@dataclass(frozen=True)
class ObjectInfo:
    """A stored object. `crc32c` is base64 or None when the store does not report it; `version` is
    the store's version token (a GCS generation, or an S3 version id or ETag)."""
    size: int
    crc32c: str | None
    version: str


def check_part_listing(parts: list[Part], next_marker: int | None, marker: int, limit: int) -> None:
    """Refuse a part listing page that is out of order, repeats or skips back, or does not advance."""
    numbers = [part.part_number for part in parts]
    if len(parts) > limit or numbers != sorted(set(numbers)) or any(n <= marker or n > 10000 for n in numbers):
        raise StorageFailure("invalid_part_listing")
    if next_marker is not None and (not parts or next_marker != numbers[-1] or next_marker <= marker):
        raise StorageFailure("invalid_part_listing")
    if any(not part.etag or part.size < 1 for part in parts):
        raise StorageFailure("invalid_part_listing")


class ObjectStore(Protocol):
    """The object store behind uploads and downloads. Keys are object names inside a bucket; every
    method raises StorageFailure (or UploadMissing for a multipart upload that no longer exists)."""

    def start_upload(self, bucket: str, key: str) -> str: ...

    def open_uploads(self, bucket: str, key: str) -> list[str]: ...

    def list_parts(self, bucket: str, key: str, upload_id: str, marker: int = 0, limit: int = 1000) -> tuple[list[Part], int | None]: ...

    def sign_part(self, bucket: str, key: str, upload_id: str, number: int, crc32c: str, size: int, expires_at: datetime) -> tuple[str, dict[str, str]]: ...

    def finish_upload(self, bucket: str, key: str, upload_id: str, parts: list[Part], crc32c_by_part: dict[int, str]) -> None: ...

    def abort_upload(self, bucket: str, key: str, upload_id: str) -> None: ...

    def head(self, bucket: str, key: str) -> ObjectInfo | None: ...

    def delete(self, bucket: str, key: str, version: str | None) -> None: ...

    def signed_download(self, bucket: str, key: str, version: str | None, filename: str, disposition: str, expires_in: timedelta) -> str: ...

    def put_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None: ...

    def get_text(self, bucket: str, key: str) -> str | None: ...
