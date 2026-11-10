from typing import Optional

from sqlmodel import Field, SQLModel

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


class IngestionRequestUpdate(SQLModel):
    """What an ingestion consumer reports back. Other fields of the request are ignored."""
    id: Optional[int] = None
    dataset_mfid: Optional[str] = None
    file_mfid: Optional[str] = None
    status: Optional[str] = Field(default=None, max_length=64)
    time_completed: Optional[str] = Field(default=None, max_length=64)
    ingestion_githash: Optional[str] = Field(default=None, max_length=128)
