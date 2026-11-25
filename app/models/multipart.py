import base64
import hashlib
from datetime import datetime
from typing import Annotated, Literal

from app.storage.registry import STORE_ID_PATTERN
from pydantic import BaseModel, ConfigDict, Field, AfterValidator, field_validator, model_validator

from app.models.datasets import AssociatedFileRead
from app.models.resource_settings import StoreWarning
from app.models.timestamps import UtcDatetime


def validate_crc32c(value: str) -> str:
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:
        raise ValueError("CRC32C must be canonical base64 for four bytes") from None
    if len(decoded) != 4 or base64.b64encode(decoded).decode() != value:
        raise ValueError("CRC32C must be canonical base64 for four bytes")
    return value


CRC32C = Annotated[str, Field(min_length=8, max_length=8), AfterValidator(validate_crc32c)]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def empty_hash(body):
    if body.size == 0 and body.sha256_hash not in (None, EMPTY_SHA256):
        raise ValueError(f"An empty file's SHA-256 is {EMPTY_SHA256}")
    return body


def checked_filename(value: str) -> str:
    if not 1 <= len(value) <= 1024:
        raise ValueError("Filename must be 1 to 1024 characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise ValueError("Invalid filename")
    if any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("Filename must be a relative path without dot segments")
    return value


class MultipartInitiate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0, le=500 * 1024 ** 3, strict=True)
    sha256_hash: SHA256 | None = None
    store_id: str | None = Field(default=None, max_length=63, pattern=STORE_ID_PATTERN, description="The store to upload to (see GET /storage/stores); by default the project's, then the instrument's, then the server's default store.")
    check_empty_hash = model_validator(mode="after")(empty_hash)

    @field_validator("filename")
    @classmethod
    def valid_filename(cls, value):
        return checked_filename(value)


class MultipartReplace(BaseModel):
    model_config = ConfigDict(extra="forbid")
    size: int = Field(ge=0, le=500 * 1024 ** 3, strict=True)
    sha256_hash: SHA256 | None = None
    store_id: str | None = Field(default=None, max_length=63, pattern=STORE_ID_PATTERN, description="The store to upload the new content to; by default the file's current store.")
    check_empty_hash = model_validator(mode="after")(empty_hash)


class MultipartPart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    part_number: int = Field(ge=1, le=10000, strict=True)
    crc32c: CRC32C


class MultipartParts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parts: list[MultipartPart] = Field(min_length=1, max_length=64)

    @field_validator("parts")
    @classmethod
    def unique_parts(cls, value):
        if len({part.part_number for part in value}) != len(value):
            raise ValueError("Duplicate part numbers")
        return value


class MultipartComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_id: str = Field(min_length=1, max_length=128)
    sha256_hash: SHA256
    crc32c: CRC32C


class UploadedPart(BaseModel):
    part_number: int
    size: int
    etag: str


class MultipartStatus(BaseModel):
    upload_id: str | None = None
    upload_protocol: Literal["xml-multipart-signed-v1"] = "xml-multipart-signed-v1"
    store_id: str | None = Field(default=None, description="The store this upload goes to (see GET /storage/stores)")
    state: str
    expires_at: datetime | None = None
    part_size: int | None = None
    part_count: int | None = None
    parts: list[UploadedPart] = Field(default_factory=list)
    next_part_number_marker: int | None = None
    existing_file: AssociatedFileRead | None = None
    failure_code: str | None = None
    warnings: list[StoreWarning] = Field(default_factory=list, description="On initiation: a project or instrument default store that is not configured any more and was skipped (default_store_unavailable)")


class SignedPart(BaseModel):
    part_number: int
    url: str
    headers: dict[str, str]
    offset: int
    size: int
    expires_at: UtcDatetime


class SignedParts(BaseModel):
    parts: list[SignedPart]
