from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.timestamps import UtcDatetime

RESOURCE_SETTINGS_SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 16 * 1024


class ResourceSettingsDocument(BaseModel):
    '''Settings shared by every record type that has them.'''
    model_config = ConfigDict(extra="forbid", strict=True)

    default_store_id: Optional[str] = None

    @field_validator("default_store_id")
    @classmethod
    def configured_store(cls, value):
        from app.config import get_settings

        if value is not None and value not in {store.id for store in get_settings().storage.stores}:
            raise ValueError(f"unknown_store: {value} is not a store on this server (see GET /storage/stores)")
        return value


class ProjectSettings(ResourceSettingsDocument):
    '''A project's settings. default_store_id: where uploads to the project's datasets go unless the upload names a store.'''


class InstrumentSettings(ResourceSettingsDocument):
    '''An instrument's settings. default_store_id: where uploads to the instrument's datasets go when neither the upload nor the project names a store.'''


class StoreWarning(BaseModel):
    code: Literal["default_store_unavailable"]
    store_id: str
    record_mfid: Optional[str] = None
    message: str


def unavailable_default(store_id: Optional[str], record_mfid: str) -> Optional[StoreWarning]:
    """A warning when a record's default store is not configured on this server any more; uploads then skip it."""
    from app.config import get_settings

    if store_id is None or get_settings().storage.store(store_id) is not None:
        return None
    return StoreWarning(code="default_store_unavailable", store_id=store_id, record_mfid=record_mfid, message=f"Default store {store_id} of {record_mfid} is not configured on this server; uploads use the next default")


class ResourceSettingsRead(BaseModel):
    schema_version: int
    revision: int
    updated_at: Optional[UtcDatetime] = None
    settings: dict[str, Any]
    warnings: list[StoreWarning] = Field(default_factory=list, description="Settings this server cannot honour, such as a default_store_id no longer configured (default_store_unavailable)")
