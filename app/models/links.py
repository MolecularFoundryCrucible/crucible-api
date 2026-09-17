from enum import Enum
from typing import Literal, Optional

from sqlmodel import SQLModel


class LinkRelationshipType(str, Enum):
    IS_DERIVED_FROM = "is_derived_from"
    IS_PART_OF = "is_part_of"


class LinkedResource(SQLModel):
    unique_id: str
    resource_type: str
    name: Optional[str] = None
    direction: Literal["source", "target", "undirected"]
    relationship_type: Optional[str] = None
