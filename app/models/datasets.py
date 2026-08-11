from typing import Any, List, Optional
from pydantic import field_validator
from sqlmodel import SQLModel
from app.general_utils import blank_to_none, is_valid_mfid
from app.models.links import LinkedResource


class DatasetCreate(SQLModel):
    '''
    Fields accepted when creating or upserting a dataset.
    Provide unique_id to create a record under a pre-assigned ID (must be a
    valid mfid, e.g. one generated client-side by an instrument using the
    same mfid library); omit it to auto-generate one.
    '''
    unique_id: Optional[str] = None

    @field_validator("unique_id")
    @classmethod
    def check_unique_id(cls, v):
        if v is not None and not is_valid_mfid(v):
            raise ValueError("unique_id must be a valid mfid: 26-character lowercase Crockford's Base32")
        return v
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

    @field_validator("project_id", "owner_orcid", mode="before", check_fields=False)
    @classmethod
    def blank_identifiers_to_none(cls, v):
        """Uploaders that send "" instead of omitting the field would otherwise
        attach the resource to a nameless project. See general_utils."""
        return blank_to_none(v)

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

    @field_validator("project_id", "owner_orcid", mode="before", check_fields=False)
    @classmethod
    def blank_identifiers_to_none(cls, v):
        """Uploaders that send "" instead of omitting the field would otherwise
        attach the resource to a nameless project. See general_utils."""
        return blank_to_none(v)

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
    storage_backend defaults to "gcs"; any other value marks this as a catalog-only
    reference to a file living outside GCS (e.g. an HPC partition or Globus path) —
    Crucible does not verify existence or broker access for non-gcs files.
    '''
    filename: str
    size: int
    sha256_hash: str
    storage_backend: str = "gcs"
    access_note: Optional[str] = None


class AssociatedFileRead(SQLModel):
    '''Response model for AssociatedFile.'''
    mfid: str
    dataset_id: int
    dataset_mfid: str
    filename: str
    storage_path: Optional[str] = None
    storage_backend: str
    access_note: Optional[str] = None
    size: int
    sha256_hash: str


class AssociatedFileUpdate(SQLModel):
    '''Partial update schema for AssociatedFile. Only provided fields are modified.'''
    filename: Optional[str] = None
    storage_path: Optional[str] = None
    storage_backend: Optional[str] = None
    access_note: Optional[str] = None
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
    owner: Optional[Any] = None  # UserPublicRead when include_owner=true

