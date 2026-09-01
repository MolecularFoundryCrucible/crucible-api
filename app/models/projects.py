import warnings
from enum import Enum
from typing import List, Optional
from pydantic import ConfigDict, field_validator, model_validator
from sqlmodel import SQLModel
from app.models.access_groups import AccessGroupMemberRead
from app.models.users import UserPublicRead


class ProjectMembershipRole(str, Enum):
    VIEWER = "viewer"
    CONTRIBUTOR = "contributor"
    EDITOR = "editor"
    ADMIN = "admin"
    OWNER = "owner"


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
    project_lead: Optional[str] = None  # ORCID, username, or email; resolved server-side.
                                         # Prefer over the three project_lead_* fields below,
                                         # kept for existing integrations. Provide at most one.
    project_lead_orcid: Optional[str] = None
    project_lead_email: Optional[str] = None
    project_lead_username: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None


class ProjectRead(SQLModel):
    '''Response model for Project.'''
    # id: int
    unique_id: str
    project_id: str
    organization: str
    project_lead_orcid: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None

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
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None
    members: Optional[List[AccessGroupMemberRead]] = None


class ProjectMinimalRead(SQLModel):
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

    project_id: Optional[str] = None
    organization: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None

    @field_validator("project_id")
    @classmethod
    def check_project_id(cls, v):
        from app.utils.project_utils import validate_project_id
        return validate_project_id(v) if v is not None else v
