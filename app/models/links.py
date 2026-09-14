from typing import Literal, Optional
from sqlmodel import SQLModel


class LinkedResource(SQLModel):
    unique_id: str
    resource_type: str
    name: Optional[str] = None
    direction: Literal["source", "target", "undirected"]
    relationship_type: Optional[str] = None
