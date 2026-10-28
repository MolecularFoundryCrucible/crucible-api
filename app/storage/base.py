from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class StorageFailure(Exception):
    pass


class UploadMissing(StorageFailure):
    pass


@dataclass(frozen=True)
class Part:
    part_number: int
    size: int
    etag: str


@dataclass(frozen=True)
class ObjectInfo:
    """A stored object. `crc32c` is base64 or None when the store does not report it; `version` is
    the store's version token (a GCS generation, or an S3 version id or ETag)."""
    size: int
    crc32c: str | None
    version: str


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
