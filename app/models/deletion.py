from enum import Enum
from typing import Optional

from pydantic import Field
from sqlmodel import SQLModel


class DeletionRequestScope(str, Enum):
    ACCESSIBLE = "accessible"
    SUBMITTED = "submitted"
    REVIEWABLE = "reviewable"


class DeletionRequestCapabilities(SQLModel):
    can_review: bool = Field(
        default=False,
        description="Whether the caller may approve or reject this pending request.",
    )


class DeletionRequestRead(SQLModel):
    """Response model for DeletionRequest that includes resource_type and project_id."""
    id: int
    resource_id: str
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    project_id: Optional[str] = None
    requester_id: str
    reason: Optional[str] = None
    status: str
    request_time: Optional[str] = None
    review_time: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_notes: Optional[str] = None
    capabilities: DeletionRequestCapabilities = Field(
        default_factory=DeletionRequestCapabilities
    )


class DeletionAuditLogRead(SQLModel):
    """Response model for DeletionAuditLog."""
    id: int
    resource_id: str
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    project_id: Optional[str] = None
    requester_id: str
    reason: Optional[str] = None
    request_time: Optional[str] = None
    reviewer_id: str
    reviewer_notes: Optional[str] = None
    review_time: Optional[str] = None
    deleted_at: str
