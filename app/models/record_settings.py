from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from app.models.timestamps import UtcDatetime

RECORD_SETTINGS_SCHEMA_VERSION = 1
MAX_SETTINGS_BYTES = 16 * 1024


class RecordSettings(BaseModel):
    '''Settings shared by every record type that has them.'''
    model_config = ConfigDict(extra="forbid", strict=True)

    default_store: Optional[str] = None

    @field_validator("default_store")
    @classmethod
    def configured_store(cls, value):
        from app.config import get_settings

        if value is not None and value not in {store.id for store in get_settings().storage.stores}:
            raise ValueError(f"unknown_store: {value} is not a store on this server (see GET /storage/stores)")
        return value


class ProjectSettings(RecordSettings):
    '''A project's settings. default_store: where uploads to the project's datasets go unless the upload names a store.'''


class InstrumentSettings(RecordSettings):
    '''An instrument's settings. default_store: where uploads to the instrument's datasets go when neither the upload nor the project names a store.'''


class RecordSettingsRead(BaseModel):
    schema_version: int
    revision: int
    updated_at: Optional[UtcDatetime] = None
    settings: dict[str, Any]
