from typing import Optional
from sqlmodel import Field, SQLModel, Relationship
from app.general_utils import get_tz_isoformat

class DeletionRequestRead(SQLModel):
    """Response model for DeletionRequest that includes resource_type from Resource."""
    id: int
    resource_id: str
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    requester_id: str
    reason: Optional[str] = None
    status: str
    request_time: Optional[str] = None
    review_time: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_notes: Optional[str] = None

    
class DeletionRequest(SQLModel, table=True):
    """
    Tracks a request to soft-delete a Dataset or Sample.
    An admin must approve or reject it before the entity is actually hidden.
    """

    id: Optional[int] = Field(default=None, primary_key=True)

    # What is being deleted
    #resource_type: str = Field(index=True)
    resource_id: str = Field(foreign_key = 'idtype.mfid', index=True)
    resource: Optional["Resource"] = Relationship(back_populates="deletion_requests",
                                                sa_relationship_kwargs={"foreign_keys": 
                                                                        "[DeletionRequest.resource_id]"})

    # Human-readable name of the resource at the time of the request
    resource_name: Optional[str] = Field(default=None)

    # Who requested it and why
    requester_id: str = Field(index=True)
    reason: Optional[str] = Field(default=None)

    # Workflow status
    # Allowed values: "pending", "approved", "rejected"
    status: str = Field(default="pending", index=True)

    # Timestamps
    request_time: Optional[str] = Field(default_factory=get_tz_isoformat)
    review_time:  Optional[str] = Field(default=None)

    # Who resolved it and why (filled in by admin)
    reviewer_id: Optional[str] = Field(default=None)
    reviewer_notes: Optional[str] = Field(default=None)




    


