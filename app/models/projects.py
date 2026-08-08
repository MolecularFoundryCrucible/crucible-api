import warnings
from typing import Optional, Union
from pydantic import field_validator, model_validator
from sqlmodel import SQLModel
from app.models.users import UserPublicRead, UserRead


class ProjectCreate(SQLModel):
    '''Input schema for creating a project. One of project_lead_orcid, project_lead_email, or project_lead_username must be provided.'''
    project_id: str
    organization: str

    @field_validator("project_id")
    @classmethod
    def check_project_id(cls, v):
        """Format and reserved-name check. The collision check against existing
        access groups needs a session and lives in the route."""
        from app.utils.project_utils import validate_project_id
        return validate_project_id(v)
    project_lead_orcid: Optional[str] = None
    project_lead_email: Optional[str] = None
    project_lead_username: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None


class ProjectRead(SQLModel):
    '''Response model for Project.'''
    # id: int
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
    '''ProjectRead extended with the resolved project lead. Non-members get the
    project lead as UserPublicRead (no email) and never see scientific_metadata,
    regardless of ?include_metadata=. Members and admins get the full UserRead.'''
    lead: Optional[Union[UserRead, UserPublicRead]] = None
    scientific_metadata: Optional[dict] = None
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None


class ProjectUpdate(SQLModel):
    '''Partial update schema for Project. Only provided fields are modified.
    Provide one of project_lead_orcid, project_lead_email, or project_lead_username to update the lead.'''
    organization: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None
    project_lead_orcid: Optional[str] = None
    project_lead_email: Optional[str] = None
    project_lead_username: Optional[str] = None
