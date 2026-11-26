import hashlib
from typing import List, Literal, Optional
from pydantic import MISSING, ConfigDict, Field, computed_field, field_validator, model_validator
from sqlmodel import SQLModel
from app.general_utils import fold_owner_ref, refuse_instrument_name, refuse_public_change
from app.general_utils import blank_to_none, is_valid_mfid
from app.models.fields import NonEmptyStr
from app.models.links import LinkedResource
from app.models.instrument import InstrumentReferenceRead
from app.models.access import AccessPermission
from app.models.projects import ProjectReferenceRead, ProjectRelation
from app.models.resource import RECORD_ROLE_DESCRIPTION, ActiveDeletionRequestRead
from app.models.resource import ResourceCapabilities
from app.models.timestamps import UtcDatetime
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
    project_id: Optional[str] = Field(
        default=None,
        description="Project slug. May be combined with a matching project_mfid.",
    )
    project_mfid: Optional[str] = Field(
        default=None,
        description="Canonical project MFID. May be combined with a matching project_id.",
    )
    instrument_id: Optional[str] = Field(
        default=None,
        description="Registered instrument slug. May be combined with a matching instrument_mfid.",
    )
    instrument_mfid: Optional[str] = Field(
        default=None,
        description="Canonical registered instrument MFID. May be combined with a matching instrument_id.",
    )
    data_format: Optional[str] = None
    measurement: Optional[str] = None
    data_type: Optional[str] = None
    session_name: Optional[str] = None
    timestamp: Optional[str] = None
    public: bool = False
    owner_orcid: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated canonical owner identity (human ORCID or service-account MFID). Use owner_ref.",
    )
    owner_ref: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Owner: unique_id, linked ORCID, username or email. Defaults to the authenticated identity.",
    )
    owner: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated: use owner_ref.",
    )

    @field_validator(
        "project_id",
        "project_mfid",
        "instrument_id",
        "instrument_mfid",
        "owner_orcid",
        "owner",
        "owner_ref",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def blank_identifiers_to_none(cls, v):
        """Uploaders that send "" instead of omitting the field would otherwise
        attach the resource to a nameless project. See general_utils."""
        return blank_to_none(v)

    @field_validator("instrument_mfid")
    @classmethod
    def check_instrument_mfid(cls, v):
        if v is not None and not is_valid_mfid(v):
            raise ValueError("instrument_mfid must be a valid mfid: 26-character lowercase Crockford's Base32")
        return v

    @field_validator("project_mfid")
    @classmethod
    def check_project_mfid(cls, v):
        if v is not None and not is_valid_mfid(v):
            raise ValueError("project_mfid must be a valid mfid: 26-character lowercase Crockford's Base32")
        return v

    @model_validator(mode="before")
    @classmethod
    def instrument_name_is_not_an_identifier(cls, data):
        return refuse_instrument_name(data)

    @model_validator(mode="after")
    def validate_owner_reference(self):
        return fold_owner_ref(self)

class DatasetUpdate(SQLModel):
    '''Partial update schema for Dataset. Only provided fields are modified.

    No project_id/owner_orcid — use POST /resources/{mfid}/project and
    POST /resources/{mfid}/transfer_ownership.
    '''
    model_config = ConfigDict(extra="forbid")

    dataset_name: Optional[str] = None
    instrument_id: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated for dataset updates. Only the current value is accepted. Use PUT /datasets/{dataset_mfid}/instrument.",
    )
    data_format: Optional[str] = None
    measurement: Optional[str] = None
    data_type: Optional[str] = None
    session_name: Optional[str] = None
    timestamp: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def public_is_not_editable(cls, data):
        return refuse_instrument_name(refuse_public_change(data))


class DatasetInstrumentAssign(SQLModel):
    '''Body for PUT /datasets/{dataset_mfid}/instrument.

    Either identifier resolves the instrument; supplying both is checked for
    agreement by resolve_instrument_identifiers. instrument_name is not
    accepted — it is a label derived from the resolved instrument, so letting
    a caller set it independently is how the three columns drift apart.
    '''
    model_config = ConfigDict(extra="forbid")

    instrument_id: Optional[str] = Field(
        default=None,
        description="Registered instrument slug. May be combined with a matching instrument_mfid.",
    )
    instrument_mfid: Optional[str] = Field(
        default=None,
        description="Canonical registered instrument MFID. May be combined with a matching instrument_id.",
    )

    @field_validator("instrument_id", "instrument_mfid", mode="before")
    @classmethod
    def blank_identifiers_to_none(cls, v):
        return blank_to_none(v)

    @field_validator("instrument_mfid")
    @classmethod
    def check_instrument_mfid(cls, v):
        if v is not None and not is_valid_mfid(v):
            raise ValueError("instrument_mfid must be a valid mfid: 26-character lowercase Crockford's Base32")
        return v

    @model_validator(mode="after")
    def require_an_identifier(self):
        if self.instrument_id is None and self.instrument_mfid is None:
            raise ValueError("Provide instrument_id or instrument_mfid")
        return self


class DatasetInstrumentRead(SQLModel):
    '''Result of PUT /datasets/{dataset_mfid}/instrument.'''
    dataset_mfid: str
    previous_instrument: Optional[InstrumentReferenceRead] = None
    previous_instrument_name: Optional[str] = Field(
        default=None,
        description="Name of the dataset's previous registered instrument, or null when it had none.",
    )
    instrument: InstrumentReferenceRead


class DatasetRead(SQLModel):
    '''Response model for Dataset. Includes all server-generated fields.'''
    unique_id: str
    public: bool
    dataset_name: Optional[str] = None
   # file_to_upload: Optional[str] = None
    owner_orcid: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use owner.unique_id.")
    project_id: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use project.project_id.")
    instrument_name: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use instrument.instrument_name.")
    instrument_id: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use instrument.instrument_id.")
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
    dataset_id: int = Field(
        deprecated=True,
        description="Deprecated internal dataset key. Use dataset_mfid.",
    )
    dataset_mfid: str
    thumbnail_name: str
    thumbnail_b64str: str
    mime_type: Literal["image/jpeg"] = "image/jpeg"


class ThumbnailPreviewRead(SQLModel):
    '''Thumbnail fields returned by the bulk preview endpoint.'''
    id: int
    thumbnail_name: str
    thumbnail_b64str: str
    mime_type: Literal["image/jpeg"] = "image/jpeg"


def file_etag(storage_path, storage_version, sha256_hash, size):
    identity = f"{storage_path or ''}|{storage_version or ''}|{sha256_hash}|{size}"
    return '"' + hashlib.sha256(identity.encode()).hexdigest()[:32] + '"'


def validate_non_gcs_filename(value: str) -> str:
    """A non-GCS filename is the file's identity in its dataset, and clients rebuild datasets on disk from it."""
    if not value:
        raise ValueError("filename must not be empty")
    if any(ord(character) <= 0x1F or ord(character) == 0x7F for character in value):
        raise ValueError("filename must not contain ASCII control characters")
    if value.startswith("/") or value.endswith("/") or "//" in value:
        raise ValueError("filename must be a canonical relative POSIX path")
    if any(component in {".", ".."} for component in value.split("/")):
        raise ValueError("filename must not contain '.' or '..' components")
    return value


class AssociatedFileCreate(SQLModel):
    '''Fields required to attach an associated file to a dataset.
    filename should be in the format {cloud-bucket}/{path-to-file}
    Ingestion is currently only supported for files in the crucible-uploads cloud-bucket.
    storage_backend defaults to "gcs"; any other value marks this as a catalog-only
    reference to a file living outside GCS (e.g. an HPC partition or Globus path) —
    Crucible does not verify existence or broker access for non-gcs files.
    For non-gcs files, filename must be a canonical relative POSIX path: no leading,
    trailing, or repeated "/", no "." or ".." components, and no ASCII control characters.
    '''
    filename: str
    size: int
    sha256_hash: str
    storage_backend: str = "gcs"
    storage_path: Optional[str] = Field(
        default=None,
        description="Backend-specific location for a non-GCS catalog-only file.",
    )
    access_note: Optional[str] = None

    @model_validator(mode="after")
    def validate_storage_path(self):
        if self.storage_backend == "gcs" and self.storage_path is not None:
            raise ValueError("storage_path is assigned by the upload API for GCS files")
        if self.storage_backend != "gcs":
            validate_non_gcs_filename(self.filename)
        return self


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
    storage_version: Optional[str] = Field(default=None, exclude=True)
    store_id: Optional[str] = Field(default=None, description="The store holding a stored file (see GET /storage/stores); null for a catalog-only record, or a stored file whose store has not been recorded yet.")

    @computed_field
    @property
    def etag(self) -> str:
        return file_etag(self.storage_path, self.storage_version, self.sha256_hash, self.size)

    @model_validator(mode="after")
    def catalog_records_have_no_store(self):
        if self.storage_backend != "gcs":
            self.store_id = None
        return self


class DeletedFileRead(SQLModel):
    '''A deleted file whose content is still kept and can be restored until restorable_until.'''
    file_mfid: str
    dataset_mfid: str
    filename: str
    size: int
    sha256_hash: str
    store_id: Optional[str] = None
    deleted_at: UtcDatetime
    restorable_until: UtcDatetime


class AssociatedFileUpdate(SQLModel):
    '''Partial update schema for AssociatedFile. Only provided fields are modified.'''
    filename: NonEmptyStr | MISSING = MISSING
    storage_path: Optional[str] = None
    storage_backend: NonEmptyStr | MISSING = MISSING
    access_note: Optional[str] = None
    size: int | MISSING = MISSING
    sha256_hash: NonEmptyStr | MISSING = MISSING
    

class ScientificMetadataBinaryRead(SQLModel):
    #id: Optional[int] = None
    unique_id: str
    scientific_metadata: dict


class DatasetResponse(DatasetRead):
    '''DatasetRead extended with Resource timestamps, optional scientific metadata, and deletion status.'''
    resource_type: Optional[str] = "dataset"
    creation_time: Optional[UtcDatetime] = None
    modification_time: Optional[UtcDatetime] = None
    scientific_metadata: Optional[dict] = None
    deletion_request: Optional[ActiveDeletionRequestRead] = None
    links: Optional[List[LinkedResource]] = None
    owner: Optional[UserPublicRead] = None
    capabilities: Optional[ResourceCapabilities] = None
    project_relation: Optional[ProjectRelation] = None
    role: Optional[AccessPermission] = Field(default=None, description=RECORD_ROLE_DESCRIPTION)


class KeywordRead(SQLModel):
    keyword: str
    num_datasets: Optional[int] = None
