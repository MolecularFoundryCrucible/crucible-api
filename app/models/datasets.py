from typing import List, Optional
from sqlmodel import SQLModel
from app.models.links import LinkedResource


class DatasetCreate(SQLModel):
    '''
    Fields accepted when creating or upserting a dataset.
    Provide unique_id to create a record under a pre-assigned ID; omit it to auto-generate one.
    with an auto-generated MFID.
    '''
    unique_id: Optional[str] = None
    dataset_name: Optional[str] = None
    project_id: Optional[str] = None
    source_folder: Optional[str] = None
    instrument_name: Optional[str] = None
    data_format: Optional[str] = None
    measurement: Optional[str] = None
    data_type: Optional[str] = None
    session_name: Optional[str] = None
    timestamp: Optional[str] = None
    public: bool = False
    owner_orcid: Optional[str] = None     # falls back to the authenticated user if not provided


class DatasetUpdate(SQLModel):
    '''Partial update schema for Dataset. Only provided fields are modified.'''
    dataset_name: Optional[str] = None
    project_id: Optional[str] = None
    instrument_name: Optional[str] = None
    data_format: Optional[str] = None
    measurement: Optional[str] = None
    data_type: Optional[str] = None
    session_name: Optional[str] = None
    timestamp: Optional[str] = None
    public: Optional[bool] = None
    owner_orcid: Optional[str] = None


class DatasetRead(SQLModel):
    '''Response model for Dataset. Includes all server-generated fields.'''
    unique_id: str
    public: bool
    dataset_name: Optional[str] = None
   # file_to_upload: Optional[str] = None
    owner_orcid: Optional[str] = None
    project_id: Optional[str] = None
    source_folder: Optional[str] = None
    instrument_name: Optional[str] = None
    timestamp: Optional[str] = None
    size: Optional[int] = None
    data_format: Optional[str] = None
    measurement: Optional[str] = None
    data_type: Optional[str] = None
    session_name: Optional[str] = None
   # sha256_hash_file_to_upload: Optional[str] = None


class ThumbnailCreate(SQLModel):
    '''Fields required to attach a thumbnail to a dataset.'''
    thumbnail_name: str
    thumbnail_b64str: str


class ThumbnailRead(SQLModel):
    '''Response model for Thumbnail.'''
    id: int
    dataset_id: int
    thumbnail_name: str
    thumbnail_b64str: str


class AssociatedFileCreate(SQLModel):
    '''Fields required to attach an associated file to a dataset.
    filename should be in the format {cloud-bucket}/{path-to-file}
    Ingestion is currently only supported for files in the crucible-uploads cloud-bucket. 
    '''
    filename: str
    size: int
    sha256_hash: str


class AssociatedFileRead(SQLModel):
    '''Response model for AssociatedFile.'''
    mfid: str
    dataset_id: int
    dataset_mfid: str
    filename: str
    storage_path: Optional[str] = None
    size: int
    sha256_hash: str


class AssociatedFileUpdate(SQLModel):
    '''Partial update schema for AssociatedFile. Only provided fields are modified.'''
    filename: Optional[str] = None
    storage_path: Optional[str] = None
    size: Optional[int] = None
    sha256_hash: Optional[str] = None
    

class ScientificMetadataBinaryRead(SQLModel):
    #id: Optional[int] = None
    unique_id: str
    scientific_metadata: dict


class DatasetResponse(DatasetRead):
    '''DatasetRead extended with Resource timestamps, optional scientific metadata, and deletion status.'''
    resource_type: Optional[str] = "dataset"
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None
    scientific_metadata: Optional[dict] = None
    deletion_request: Optional[dict] = None
    links: Optional[List[LinkedResource]] = None

