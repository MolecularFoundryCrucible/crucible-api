from typing import Literal, Optional

from sqlmodel import Field, SQLModel

from app.models.timestamps import UtcDatetime

INGESTION_CLASS_PATTERN = r"^[A-Za-z0-9_.-]{1,128}$"


class IngestionRequestRead(SQLModel):
    id: int
    dataset_mfid: Optional[str] = None
    file_mfid: Optional[str] = None
    file_to_upload: Optional[str] = None
    ingestion_githash: Optional[str] = None
    ingestion_class: Optional[str] = None
    time_created: Optional[str] = None
    time_completed: Optional[str] = None
    status: Optional[str] = None
    published_at: Optional[UtcDatetime] = Field(default=None, description="When the message queue took the request; null until then, while it is sent again automatically")


class IngestWarning(SQLModel):
    code: Literal["no_live_worker", "store_not_read", "queue_deferred"]
    message: str
    ingestion_class: Optional[str] = None
    bucket: Optional[str] = None


class IngestionRequestCreated(IngestionRequestRead):
    warnings: list[IngestWarning] = Field(default_factory=list, description="The request is queued either way: no_live_worker (no live worker offers the ingestor, or none is live), store_not_read (no live worker reads the file's bucket), queue_deferred (the message queue did not take it now; it is sent again automatically)")


class IngestionRequestUpdate(SQLModel):
    """What an ingestion consumer reports back. Other fields of the request are ignored."""
    id: Optional[int] = None
    dataset_mfid: Optional[str] = None
    file_mfid: Optional[str] = None
    status: Optional[str] = Field(default=None, max_length=64)
    time_completed: Optional[str] = Field(default=None, max_length=64)
    ingestion_githash: Optional[str] = Field(default=None, max_length=128)
