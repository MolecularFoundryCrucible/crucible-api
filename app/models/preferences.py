import json
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.models.timestamps import UtcDatetime

PREFERENCES_SCHEMA_VERSION = 1
MAX_NAMESPACE_BYTES = 4 * 1024
MAX_DOCUMENT_BYTES = 16 * 1024


def serialized_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode())


class PreferencesDocument(BaseModel):
    '''Validated preference document. Shared keys are typed; client namespaces are opaque objects.'''
    model_config = ConfigDict(extra="forbid", strict=True)

    theme: Optional[Literal["light", "dark", "system"]] = None
    default_record_kind: Optional[Literal["samples", "datasets"]] = None
    record_recent_resources: Optional[bool] = None
    hub: Optional[dict[str, Any]] = None

    @field_validator("hub")
    @classmethod
    def check_namespace_size(cls, value):
        if value is not None and serialized_size(value) > MAX_NAMESPACE_BYTES:
            raise ValueError(f"namespace exceeds {MAX_NAMESPACE_BYTES} bytes")
        return value


class PreferencesRead(BaseModel):
    schema_version: int
    revision: int
    updated_at: Optional[UtcDatetime] = None
    preferences: dict[str, Any]


class RecentResourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_mfid: str = Field(min_length=1, max_length=64)


class RecentResourceRead(BaseModel):
    resource_mfid: str
    resource_type: Literal["dataset", "sample", "project", "instrument"]
    name: Optional[str] = None
    project_id: Optional[str] = None
    viewed_at: UtcDatetime
