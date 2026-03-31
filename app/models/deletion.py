from typing import Optional
from sqlmodel import Field, SQLModel

from app.general_utils import get_tz_isoformat


class DeletionRequest(SQLModel, table=True):
    """
    Tracks a request to soft-delete a Dataset or Sample.
    An admin must approve or reject it before the entity is actually hidden.
    """

    id: Optional[int] = Field(default=None, primary_key=True)

    # What is being deleted
    resource_type: str = Field(index=True)
    resource_id:   str = Field(index=True)

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
