from enum import Enum
from typing import Optional

from pydantic import Field
from sqlmodel import SQLModel

from app.models.users import UserPublicRead


class DeletionRequestScope(str, Enum):
    ACCESSIBLE = "accessible"
    SUBMITTED = "submitted"
    REVIEWABLE = "reviewable"


class DeletionRequestSort(str, Enum):
    REQUEST_TIME = "request_time"
    RESOURCE_NAME = "resource_name"
    PROJECT_ID = "project_id"
    REQUESTER_NAME = "requester_name"
    STATUS = "status"


class DeletionRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DeletionReviewDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class DeletableResourceType(str, Enum):
    DATASET = "dataset"
    SAMPLE = "sample"


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
    requester: Optional[UserPublicRead]
    reason: Optional[str] = None
    status: DeletionRequestStatus
    request_time: Optional[str] = None
    review_time: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer: Optional[UserPublicRead]
    reviewer_notes: Optional[str] = None
    capabilities: DeletionRequestCapabilities


class DeletionAuditLogRead(SQLModel):
    """Response model for DeletionAuditLog."""
    id: int
    resource_id: str
    resource_type: Optional[DeletableResourceType] = None
    resource_name: Optional[str] = None
    project_id: Optional[str] = None
    project_mfid: Optional[str] = Field(
        default=None,
        description=(
            "Canonical project MFID captured when the resource was deleted. "
            "Entries created before this field was introduced may be null."
        ),
    )
    requester_id: str
    requester: Optional[UserPublicRead] = Field(
        default=None,
        description="Current public-safe requester identity when the account still exists.",
    )
    reason: Optional[str] = None
    request_time: Optional[str] = None
    reviewer_id: str
    reviewer: Optional[UserPublicRead] = Field(
        default=None,
        description="Current public-safe reviewer identity when the account still exists.",
    )
    reviewer_notes: Optional[str] = None
    review_time: Optional[str] = None
    deleted_at: str
