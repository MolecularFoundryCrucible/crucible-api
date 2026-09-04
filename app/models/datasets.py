from typing import List, Literal, Optional
from pydantic import ConfigDict, Field, field_validator, model_validator
from sqlmodel import SQLModel
from app.general_utils import blank_to_none, is_valid_mfid
from app.models.links import LinkedResource
from app.models.instrument import InstrumentReferenceRead
from app.models.projects import ProjectReferenceRead
from app.models.resource import ResourceCapabilities
from app.models.users import UserPublicRead


class DatasetCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")

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
    instrument_name: Optional[str] = None
    instrument_id: Optional[str] = None  # instrument's validated slug; preferred over instrument_name when given
    data_format: Optional[str] = None
    measurement: Optional[str] = None
    data_type: Optional[str] = None
    session_name: Optional[str] = None
    timestamp: Optional[str] = None
    public: bool = False
    owner_orcid: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated canonical owner identity (human ORCID or service-account MFID). Use owner.",
    )
    owner: Optional[str] = Field(
        default=None,
        description="Owner reference: ORCID, service-account MFID, username, or email. Defaults to the authenticated identity.",
    )

    @field_validator("project_id", "owner_orcid", "owner", mode="before", check_fields=False)
    @classmethod
    def blank_identifiers_to_none(cls, v):
        """Uploaders that send "" instead of omitting the field would otherwise
        attach the resource to a nameless project. See general_utils."""
        return blank_to_none(v)

    @model_validator(mode="after")
    def validate_owner_reference(self):
        values = self.model_dump()
        if values.get("owner") is not None and values.get("owner_orcid") is not None:
            raise ValueError("Provide owner or owner_orcid, not both")
        return self

class DatasetUpdate(SQLModel):
    '''Partial update schema for Dataset. Only provided fields are modified.

    No project_id/owner_orcid — use POST /resources/{mfid}/project and
    POST /resources/{mfid}/transfer_ownership.
    '''
    model_config = ConfigDict(extra="forbid")

    dataset_name: Optional[str] = None
    instrument_name: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated for dataset updates. Only the current value is accepted.",
    )
    instrument_id: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated for dataset updates. Only the current value is accepted.",
    )
    data_format: Optional[str] = None
    measurement: Optional[str] = None
    data_type: Optional[str] = None
    session_name: Optional[str] = None
    timestamp: Optional[str] = None
    public: Optional[bool] = None

class DatasetRead(SQLModel):
    '''Response model for Dataset. Includes all server-generated fields.'''
    unique_id: str
    public: bool
    dataset_name: Optional[str] = None
   # file_to_upload: Optional[str] = None
    owner_orcid: Optional[str] = None
    project_id: Optional[str] = None
    instrument_name: Optional[str] = None
    instrument_id: Optional[str] = None  # instrument's validated slug, resolved server-side
    instrument: Optional[InstrumentReferenceRead] = None
    project: Optional[ProjectReferenceRead] = None
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


class ThumbnailUpdate(SQLModel):
    '''Fields accepted when updating a thumbnail.'''
    model_config = ConfigDict(extra="forbid")

    thumbnail_name: Optional[str] = None
    thumbnail_b64str: Optional[str] = None

    @model_validator(mode="after")
    def validate_update(self):
        if self.thumbnail_name is None and self.thumbnail_b64str is None:
            raise ValueError("Provide thumbnail_name, thumbnail_b64str, or both")
        if self.thumbnail_name is not None and not self.thumbnail_name.strip():
            raise ValueError("thumbnail_name must not be blank")
        if self.thumbnail_b64str is not None and not self.thumbnail_b64str.strip():
            raise ValueError("thumbnail_b64str must not be blank")
        return self


class ThumbnailRead(SQLModel):
    '''Response model for Thumbnail.'''
    id: int
    dataset_id: int
    thumbnail_name: str
    thumbnail_b64str: str
    mime_type: Literal["image/jpeg"] = "image/jpeg"


class ThumbnailPreviewRead(SQLModel):
    '''Thumbnail fields returned by the bulk preview endpoint.'''
    id: int
    thumbnail_name: str
    thumbnail_b64str: str
    mime_type: Literal["image/jpeg"] = "image/jpeg"


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
    owner: Optional[UserPublicRead] = None
    capabilities: Optional[ResourceCapabilities] = None
