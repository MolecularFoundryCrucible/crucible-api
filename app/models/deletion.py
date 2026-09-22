from enum import Enum
from typing import Optional

from pydantic import Field
from sqlmodel import SQLModel


class DeletionRequestScope(str, Enum):
    ACCESSIBLE = "accessible"
    SUBMITTED = "submitted"
    REVIEWABLE = "reviewable"


class DeletionRequestSort(str, Enum):
    REQUEST_TIME = "request_time"


class DeletionRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DeletionReviewDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class DeletionRequestCapabilities(SQLModel):
    can_review: bool = Field(
        description="Whether the caller may approve or reject this pending request.",
    )


class DeletionRequestRead(SQLModel):
    """Deletion request with resource, project, and caller capability context."""
    id: int
    resource_id: str
    resource_type: Optional[str] = None
    resource_name: Optional[str] = None
    project_id: Optional[str] = None
    project_mfid: Optional[str] = None
    requester_id: str
    reason: Optional[str] = None
    status: DeletionRequestStatus
    request_time: Optional[str] = None
    review_time: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_notes: Optional[str] = None
    capabilities: DeletionRequestCapabilities


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
