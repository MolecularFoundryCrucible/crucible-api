import warnings
from enum import Enum
from typing import List, Literal, Optional
from pydantic import MISSING, ConfigDict, Field, field_validator, model_validator
from sqlmodel import SQLModel
from app.models.access_groups import AccessGroupMemberRead
from app.models.fields import NonBlankStr
from app.models.resource import ResourceCapabilities
from app.models.timestamps import UtcDatetime
from app.models.users import UserPublicRead


class ProjectMembershipRole(str, Enum):
    VIEWER = "viewer"
    CONTRIBUTOR = "contributor"
    EDITOR = "editor"
    ADMIN = "admin"
    OWNER = "owner"


ProjectScope = Literal["assigned", "shared", "all"]
ProjectRelation = Literal["assigned", "shared"]


class ProjectCreate(SQLModel):
    '''Input schema for creating a project. One of project_lead, project_lead_orcid,
    project_lead_email, or project_lead_username must be provided.'''
    project_id: str
    organization: str

    @field_validator("project_id")
    @classmethod
    def check_project_id(cls, v):
        """Format and reserved-name check. The collision check against existing
        access groups needs a session and lives in the route."""
        from app.utils.project_utils import validate_project_id
        return validate_project_id(v)
    owner_ref: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Owner: unique_id, linked ORCID, username or email. Only an administrator may name someone other than the caller.",
    )
    project_lead: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use owner_ref.")
    project_lead_orcid: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use owner_ref.")
    project_lead_email: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use owner_ref.")
    project_lead_username: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use owner_ref.")
    status: Optional[str] = None
    title: Optional[str] = None

    @model_validator(mode="after")
    def fold_owner_ref(self):
        if self.owner_ref is not None:
            if self.project_lead not in (None, self.owner_ref) or any((self.project_lead_orcid, self.project_lead_email, self.project_lead_username)):
                raise ValueError("Provide only owner_ref, not project_lead fields")
            self.project_lead = self.owner_ref
        return self


class ProjectRead(SQLModel):
    '''Response model for Project.'''
    # id: int
    unique_id: str
    project_id: str
    organization: str
    project_lead_orcid: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None
    proposal_sync: bool = False
    capabilities: Optional[ResourceCapabilities] = None

    @model_validator(mode="after")
    def warn_missing_lead(self):
        if not self.project_lead_orcid:
            warnings.warn(f"Project '{self.project_id}' has no project_lead_orcid", stacklevel=2)
        return self


class ProjectWithLead(ProjectRead):
    '''ProjectRead extended with a public-safe lead profile. Project metadata and
    members remain restricted to project members and platform administrators.'''
    lead: Optional[UserPublicRead] = None
    scientific_metadata: Optional[dict] = None
    creation_time: Optional[UtcDatetime] = None
    modification_time: Optional[UtcDatetime] = None
    last_activity_at: Optional[UtcDatetime] = None
    members: Optional[List[AccessGroupMemberRead]] = None
    deletion_request: Optional[dict] = None
    role: Optional[str] = Field(
        default=None,
        description=(
            "Project membership role. On GET /projects and project singletons it is the caller's role; "
            "on GET /users/{user_id}/projects it is that user's role. Null when not a member."
        ),
    )


class ProjectReferenceRead(SQLModel):
    unique_id: str
    project_id: str
    title: Optional[str] = None


class ProjectUpdate(SQLModel):
    '''Partial update schema for Project. Only provided fields are modified.

    No project_lead_orcid/email/username: leadership changes only through
    POST /resources/{proj_id}/transfer_ownership, which moves the owner
    standing and the denormalized lead pointer atomically — a bare field
    edit here never touched the actual owner standing at all. extra="forbid"
    means sending one of those fields 422s at the schema layer rather than
    being silently dropped or requiring a runtime check in the route.
    '''
    model_config = ConfigDict(extra="forbid")

    project_id: str | MISSING = MISSING
    organization: NonBlankStr | MISSING = MISSING
    status: Optional[str] = None
    title: Optional[str] = None
    proposal_sync: bool | MISSING = Field(default=MISSING, description="Administrators only. Whether people named on a proposal with this project's `project_id` are added to it.")

    @field_validator("project_id")
    @classmethod
    def check_project_id(cls, v):
        from app.utils.project_utils import validate_project_id
        return validate_project_id(v) if v is not None else v
