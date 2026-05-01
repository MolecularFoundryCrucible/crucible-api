import warnings
from typing import Optional
from pydantic import model_validator
from sqlmodel import SQLModel
from app.models.users import UserRead


class ProjectCreate(SQLModel):
    '''Input schema for creating a project. Either project_lead_orcid or project_lead_email must be provided.'''
    project_id: str
    organization: str
    project_lead_orcid: Optional[str] = None
    project_lead_email: Optional[str] = None
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
    '''ProjectRead extended with the resolved project lead User, if requested.'''
    lead: Optional[UserRead] = None
    scientific_metadata: Optional[dict] = None
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None


class ProjectUpdate(SQLModel):
    '''Partial update schema for Project. Only provided fields are modified.
    Provide either project_lead_orcid or project_lead_email to update the lead.'''
    organization: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None
    project_lead_orcid: Optional[str] = None
    project_lead_email: Optional[str] = None
