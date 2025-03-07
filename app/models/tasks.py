from typing import Optional, List
from sqlalchemy import JSON, Column,BigInteger
from sqlmodel import Field, SQLModel,Relationship

class TransferRequest(SQLModel, table = True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(default = None, index = True)
    time_created: Optional[str] = None
    time_completed: Optional[str] = None
    source_path: str
    destination_path: str




