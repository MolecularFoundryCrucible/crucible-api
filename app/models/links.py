from typing import Literal, Optional
from sqlmodel import SQLModel


class LinkedResource(SQLModel):
    unique_id: str
    resource_type: str
    name: Optional[str] = None
    relationship: Literal["parent", "child", "associated"]
