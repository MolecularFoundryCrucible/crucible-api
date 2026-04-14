from typing import Optional
from sqlmodel import SQLModel


class SampleCreate(SQLModel):
    '''Fields accepted when registering a new sample.'''
    sample_name: str
    sample_type: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None
    owner_orcid: Optional[str] = None  # falls back to the authenticated user if not provided


class SampleUpdate(SQLModel):
    '''Partial update schema for Sample. Only provided fields are modified.'''
    sample_name: Optional[str] = None
    sample_type: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None


class SampleRead(SQLModel):
    '''Response model for Sample. Includes server-generated fields.'''
    id: int
    unique_id: str
    sample_name: str
    sample_type: Optional[str] = None
    owner_orcid: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None
