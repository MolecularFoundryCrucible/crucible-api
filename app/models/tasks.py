
from typing import Optional, List
from sqlalchemy import JSON, Column,BigInteger
from sqlmodel import Field, SQLModel,Relationship


class SciCatUpdateRequest(SQLModel, table = True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(default = None, index = True)
    time_created: Optional[str] = None
    time_completed: Optional[str] = None
    status: Optional[str] = None

class IngestionRequest(SQLModel, table = True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(default = None, index = True)
    file_to_upload: str = None
    time_created: Optional[str] = None
    time_completed: Optional[str] = None
    status: Optional[str] = None
