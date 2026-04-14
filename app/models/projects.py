from typing import Optional
from sqlmodel import SQLModel
from app.models.users import UserRead


class ProjectCreate(SQLModel):
    '''Input schema for creating a project. project_lead_orcid is required.'''
    project_id: str
    organization: str
    project_lead_orcid: str
    status: Optional[str] = None
    title: Optional[str] = None


class ProjectRead(SQLModel):
    '''Response model for Project.'''
    id: int
    project_id: str
    organization: str
    project_lead_orcid: str
    status: Optional[str] = None
    title: Optional[str] = None


class ProjectWithLead(ProjectRead):
    '''ProjectRead extended with the resolved project lead User, if requested.'''
    lead: Optional[UserRead] = None


class ProjectUpdate(SQLModel):
    '''Partial update schema for Project. Only provided fields are modified.'''
    organization: Optional[str] = None
    status: Optional[str] = None
    title: Optional[str] = None
    project_lead_orcid: Optional[str] = None
