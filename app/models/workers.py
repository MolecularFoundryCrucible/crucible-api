from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models.ingestion import INGESTION_CLASS_PATTERN
from app.models.timestamps import UtcDatetime

WORKER_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
BUCKET_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,221}$"
WorkerKind = Literal["ingestion"]


class WorkerRegister(BaseModel):
    """What a worker can do. capabilities: for kind ingestion, the ingestor names it runs. buckets: the buckets it can read."""
    model_config = ConfigDict(extra="forbid")

    kind: WorkerKind
    capabilities: list[Annotated[str, StringConstraints(pattern=INGESTION_CLASS_PATTERN)]] = Field(default_factory=list, max_length=500)
    buckets: list[Annotated[str, StringConstraints(pattern=BUCKET_PATTERN)]] = Field(default_factory=list, max_length=50)
    worker_version: Optional[str] = Field(None, max_length=128)
    package_version: Optional[str] = Field(None, max_length=128, description="For kind ingestion, the crucible-ingestion version")


class WorkerRead(BaseModel):
    worker_id: str
    kind: str
    capabilities: list[str]
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

