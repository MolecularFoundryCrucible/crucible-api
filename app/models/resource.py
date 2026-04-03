from typing import Optional, List
from sqlmodel import Field, SQLModel, Relationship
from pydantic import ConfigDict
from app.general_utils import get_tz_isoformat

class Resource(SQLModel, table=True):
    __tablename__ = "idtype"
    mfid: str = Field(default=None, primary_key=True, index=True, alias="unique_id")
    resource_type: str = Field(default=None, alias="object_type")
    creation_time: Optional[str] = Field(default_factory=get_tz_isoformat)
    modification_time: Optional[str] = Field(default_factory=get_tz_isoformat)
    deletion_requests: List["DeletionRequest"] = Relationship(back_populates = 'resource',
                                                                sa_relationship_kwargs={
                                                                    "foreign_keys": "[DeletionRequest.resource_id]"
                                                                })
    model_config = ConfigDict(populate_by_name=True)

    


