from typing import Optional
from pydantic import field_validator
from sqlmodel import SQLModel


class JoinRequestCreate(SQLModel):
    """Body for POST /projects/{proj_id}/join."""
    reason: Optional[str] = None


class JoinRequestReview(SQLModel):
    """Body for PATCH /projects/{proj_id}/join_requests/{request_id}."""
    status: str
    reviewer_notes: Optional[str] = None

    @field_validator("status")
    @classmethod
    def check_status(cls, v):
        if v not in ("approved", "rejected"):
            raise ValueError("status must be 'approved' or 'rejected'")
        return v


class JoinRequestRead(SQLModel):
    """Response model for AccessGroupJoinRequest."""
    id: int
    group_name: str
    requester_id: str
    reason: Optional[str] = None
    status: str
    request_time: Optional[str] = None
    review_time: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_notes: Optional[str] = None
