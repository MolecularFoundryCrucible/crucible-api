from enum import Enum
from typing import Literal, Optional

from pydantic import ConfigDict, Field, model_validator
from sqlmodel import SQLModel

from app.models.projects import ProjectReferenceRead
from app.models.users import UserPublicRead
from app.models.timestamps import UtcDatetime

MAX_TEXT_LENGTH = 2000


class JoinRequestStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class JoinRequestScope(str, Enum):
    ACCESSIBLE = "accessible"
    SUBMITTED = "submitted"
    REVIEWABLE = "reviewable"


class JoinRequestSort(str, Enum):
    REQUEST_TIME = "request_time"
    REVIEW_TIME = "review_time"
    STATUS = "status"
    PROJECT = "project"
    REQUESTER_NAME = "requester_name"


class JoinRequestFacetField(str, Enum):
    STATUS = "status"
    PROJECT = "project"
    REQUESTER = "requester"


class JoinRequestRole(str, Enum):
    VIEWER = "viewer"
    CONTRIBUTOR = "contributor"
    EDITOR = "editor"
    ADMIN = "admin"


class JoinRequestCreate(SQLModel):
    """Body for POST /projects/{project_ref}/join_requests."""
    model_config = ConfigDict(extra="forbid")

    reason: Optional[str] = Field(default=None, max_length=MAX_TEXT_LENGTH)


class JoinRequestReview(SQLModel):
    """Body for PATCH /join_requests/{request_id}."""
    model_config = ConfigDict(extra="forbid")

    status: Literal["approved", "rejected"]
    role: Optional[JoinRequestRole] = Field(
        default=None,
        description="Role granted on approval, strictly below the reviewer's own role. Defaults to contributor. Only with status approved.",
    )
    reviewer_notes: Optional[str] = Field(default=None, max_length=MAX_TEXT_LENGTH)

    @model_validator(mode="after")
    def role_only_on_approval(self):
        if self.role is not None and self.status != "approved":
            raise ValueError("role can only be given when approving")
        return self


class JoinRequestCapabilities(SQLModel):
    can_review: bool = Field(description="Whether the caller may approve or reject this pending request.")
    can_withdraw: bool = Field(description="Whether the caller may withdraw this pending request.")


class JoinRequestRead(SQLModel):
    """A request to join a project, with the people involved and what the caller may do with it."""
    id: int
    status: JoinRequestStatus
    project_mfid: str
    project: Optional[ProjectReferenceRead] = Field(default=None, description="Null when the project no longer exists.")
    requester_id: str
    requester: Optional[UserPublicRead] = None
    reason: Optional[str] = None
    request_time: UtcDatetime
    reviewer_id: Optional[str] = None
    reviewer: Optional[UserPublicRead] = None
    review_time: Optional[UtcDatetime] = None
    reviewer_notes: Optional[str] = None
    granted_role: Optional[str] = None
    capabilities: JoinRequestCapabilities
    group_name: Optional[str] = Field(default=None, deprecated=True, description="Deprecated. Use project.project_id.")
