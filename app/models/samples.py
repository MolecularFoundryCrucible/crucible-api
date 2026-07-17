from typing import Any, List, Optional
from sqlmodel import SQLModel

from app.models.datasets import DatasetRead
from app.models.links import LinkedResource


class SampleCreate(SQLModel):
    '''Fields accepted when registering a new sample.

    Provide unique_id to register a sample under a pre-assigned ID; omit it to
    create a new one with an auto-generated MFID.
    '''
    unique_id: Optional[str] = None
    sample_name: str
    sample_type: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None
    public: bool = False
    owner_orcid: Optional[str] = None  # falls back to the authenticated user if not provided


class SampleUpdate(SQLModel):
    '''Partial update schema for Sample. Only provided fields are modified.'''
    sample_name: Optional[str] = None
    sample_type: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None
    public: Optional[bool] = None


class SampleRead(SQLModel):
    '''Response model for Sample. Includes server-generated fields.'''
    # id: int
    unique_id: str
    sample_name: str
    sample_type: Optional[str] = None
    owner_orcid: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None
    public: bool = False


class SampleResponse(SampleRead):
    '''SampleRead extended with Resource timestamps, associated datasets, and deletion status.'''
    resource_type: Optional[str] = "sample"
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None
    scientific_metadata: Optional[dict] = None
    datasets: Optional[List[DatasetRead]] = None
    deletion_request: Optional[dict] = None
    links: Optional[List[LinkedResource]] = None
    owner: Optional[Any] = None  # UserPublicRead when include_owner=true
