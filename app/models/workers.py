from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models.ingestion import INGESTION_CLASS_PATTERN
from app.models.timestamps import UtcDatetime

WORKER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
BUCKET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,221}$"
EXTENSION_PATTERN = r"^\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}$"
WorkerKind = Literal["ingestion"]


class Capability(BaseModel):
    """One thing a worker can do: for kind ingestion, an ingestor."""
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(pattern=INGESTION_CLASS_PATTERN)]
    description: Optional[str] = Field(None, max_length=2000)
    file_extensions: list[Annotated[str, StringConstraints(pattern=EXTENSION_PATTERN)]] = Field(default_factory=list, max_length=50, description="File name endings it handles, with the leading dot, such as .mrc or .tar.gz; compared without case")

    @field_validator("file_extensions")
    @classmethod
    def lowercase(cls, value):
        return sorted({extension.lower() for extension in value})


class IngestorDetail(BaseModel):
    name: str
    description: Optional[str] = None
    file_extensions: list[str] = Field(default_factory=list)


class WorkerRegister(BaseModel):
    """What a worker can do. capabilities: for kind ingestion, the ingestors it runs. buckets: the buckets it can read."""
    model_config = ConfigDict(extra="forbid")

    kind: WorkerKind
    capabilities: list[Capability] = Field(default_factory=list, max_length=500)
    buckets: list[Annotated[str, StringConstraints(pattern=BUCKET_PATTERN)]] = Field(default_factory=list, max_length=50)
    worker_version: Optional[str] = Field(None, max_length=128)
    package_version: Optional[str] = Field(None, max_length=128, description="For kind ingestion, the crucible-ingestion version")

    @field_validator("capabilities")
    @classmethod
    def unique_names(cls, value):
        names = [capability.name for capability in value]
        if len(names) != len(set(names)):
            raise ValueError("capability names must be unique")
        return value


class WorkerRead(BaseModel):
    worker_id: str
    kind: str
    capabilities: list[Capability]
    buckets: list[str]
    store_ids: list[str] = Field(description="The stores whose bucket the worker reads")
    worker_version: Optional[str] = None
    package_version: Optional[str] = None
    registered_at: UtcDatetime
    last_seen_at: UtcDatetime
    expires_at: UtcDatetime
    live: bool


class WorkerRegistered(WorkerRead):
    heartbeat_seconds: int = Field(description="Renew the registration this often; it expires at expires_at")

