from datetime import datetime
from typing import Any, Optional, List
from pydantic import ConfigDict
from pgvector.sqlalchemy import Vector
from sqlalchemy import JSON, Column, CheckConstraint, UniqueConstraint, BigInteger, DateTime, func, Index, text, Text, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel, Relationship
from mfid import mfid
from app.general_utils import get_utc_datetime, get_utc_isoformat


def get_mfid_value():
    x = mfid()[0]
    return(x)


class DatasetKeywordLink(SQLModel, table=True):
    keyword_id: Optional[int] = Field(
        default=None, foreign_key="keyword.id", primary_key=True,
        index=True
    )
    dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True
    )

class Keyword(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    keyword: str = Field(unique = True)
    datasets: List["Dataset"] = Relationship(back_populates="keywords", link_model=DatasetKeywordLink)
    num_datasets: Optional[int]


class Thumbnail(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    thumbnail_name: str
    thumbnail_b64str: str
    dataset_id: int = Field(foreign_key="dataset.id", index=True)
    dataset: "Dataset" = Relationship(back_populates="thumbnails")


class AssociatedFile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    mfid: str = Field(default_factory=get_mfid_value, unique=True, index=True)
    filename: str                        # relative path within the dataset: "{safe_filename}"
    storage_path: Optional[str] = None  # GCS bucket/blob path when storage_backend="gcs"; a backend-specific location string otherwise
    storage_backend: str = Field(default="gcs")  # "gcs" triggers signed-URL generation; any other value is catalog-only
    access_note: Optional[str] = None    # how to access this file when there's no self-service path (non-gcs backends)
    size: int = Field(sa_column=Column(BigInteger()))
    sha256_hash: str
    storage_generation: Optional[str] = None
    storage_crc32c: Optional[str] = None
    sha256_verified_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    dataset_id: int = Field(foreign_key="dataset.id")
    dataset: "Dataset" = Relationship(back_populates='associated_files')
    ingestion_requests: List["IngestionRequest"] = Relationship(back_populates="associated_file")


class DatasetLink(SQLModel, table = True):
    child_dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True, index=True
    )
    parent_dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True, index = True
    )
    relationship_type: Optional[str] = Field(default=None, index=True)


class DatasetSampleLink(SQLModel, table=True):
    sample_id: Optional[int] = Field(
        default=None, foreign_key="sample.id", primary_key=True, index=True
    )
    dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True, index=True
    )


class SampleLink(SQLModel, table=True):
    sample_id: Optional[int] = Field(
        default=None, foreign_key="sample.id", primary_key=True, index=True
    )
    parent_sample_id: Optional[int] = Field(
        default=None, foreign_key="sample.id", primary_key=True, index=True
    )
    relationship_type: Optional[str] = Field(default=None, index=True)

# date_created >> timestamp : this is the user defined date when the sample was created (can be null)
class Sample(SQLModel, table = True):
    id: Optional[int] = Field(default = None, primary_key = True)
    unique_id: str = Field(default_factory= get_mfid_value, index = True, unique = True)
    sample_name: str = Field(index = True, default = None) 
    sample_type: Optional[str] = Field(index = True, default = None)
    owner_orcid: Optional[str]
    owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
    timestamp: Optional[str] = Field(default_factory=get_utc_isoformat, alias="date_created")
    project_id: Optional[str] = None
    project_mfid: Optional[str] = Field(default=None, index=True, foreign_key="project.unique_id")
    description: Optional[str]
    datasets: List["Dataset"] = Relationship(back_populates="samples", link_model=DatasetSampleLink)
    resource: Optional["Resource"] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "foreign(Resource.mfid)==Sample.unique_id",
            "lazy": "noload",
            "viewonly": True,
        }
    )
    parents: List["Sample"] = Relationship(
        back_populates="children",
        link_model=SampleLink,
        sa_relationship_kwargs={
            "primaryjoin": "Sample.id==SampleLink.sample_id",
            "secondaryjoin": "Sample.id==SampleLink.parent_sample_id"
        }
    )
    children: List["Sample"] = Relationship(
        back_populates="parents",
        link_model=SampleLink,
        sa_relationship_kwargs={
            "primaryjoin": "Sample.id==SampleLink.parent_sample_id",
            "secondaryjoin": "Sample.id==SampleLink.sample_id"
        }
    )

    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True,populate_by_name=True) #type: ignore

# previous creation_time >> timestamp : this is the user defined date when the dataset was created (can be null)
class Dataset(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    unique_id: str = Field(default_factory = get_mfid_value, index=True, unique=True)
    public: bool = Field(default = False, index = True)
    dataset_name: Optional[str] = Field(default = "")   
    owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
    owner_orcid: Optional[str] = Field(default = None)
    project_id: Optional[str] = Field(default=None, index=True, foreign_key="project.project_id")
    project_mfid: Optional[str] = Field(default=None, index=True, foreign_key="project.unique_id")
    instrument_id: Optional[int]  = Field(default = None, index = True, foreign_key = "instrument.id")
    instrument_mfid: Optional[str] = Field(default=None, index=True, foreign_key="instrument.unique_id")
    source_folder: Optional[str]
    instrument_name: Optional[str]
    timestamp: Optional[str] = Field(default_factory=get_utc_isoformat)
    size: Optional[int] = Field(sa_column=Column(BigInteger())) 
    data_format: Optional[str]
    measurement: Optional[str]
    data_type: Optional[str]
    session_name: Optional[str]
    samples: List[Sample] = Relationship(back_populates="datasets", link_model = DatasetSampleLink)
    keywords: List[Keyword] = Relationship(back_populates="datasets", link_model=DatasetKeywordLink)
    thumbnails: List[Thumbnail] = Relationship(back_populates = "dataset")
    associated_files: List[AssociatedFile] = Relationship(back_populates = "dataset")
    resource: Optional["Resource"] = Relationship(sa_relationship_kwargs={"primaryjoin": "foreign(Resource.mfid)==Dataset.unique_id", "lazy": "noload", "viewonly": True})
    parents: List["Dataset"] = Relationship(
        back_populates="children",
        link_model=DatasetLink,
        sa_relationship_kwargs={
            "primaryjoin": "Dataset.id==DatasetLink.child_dataset_id",
            "secondaryjoin": "Dataset.id==DatasetLink.parent_dataset_id"
        }
    )
    children: List["Dataset"] = Relationship(
        back_populates="parents",
        link_model=DatasetLink,
        sa_relationship_kwargs={
            "primaryjoin": "Dataset.id==DatasetLink.parent_dataset_id",
            "secondaryjoin": "Dataset.id==DatasetLink.child_dataset_id"
        }
    )

    model_config = ConfigDict(arbitrary_types_allowed=True, from_attributes=True, populate_by_name=True) #type: ignore


class ResourceACL(SQLModel, table=True):
    __table_args__ = (
        Index(
            "ix_resourceacl_group_mfid_permission",
            "accessgroup_id",
            "mfid",
            "permission",
        ),
        Index(
            "one_owner_acl_per_resource",
            "mfid",
            unique=True,
            postgresql_where=text("permission = 'owner'"),
            sqlite_where=text("permission = 'owner'"),
        ),
    )

    mfid: str = Field(foreign_key="idtype.mfid", primary_key=True, index=True)
    accessgroup_id: Optional[int] = Field(
        default=None, foreign_key="accessgroup.id", primary_key=True
    )
    read: bool = Field(index=True)
    write: bool = Field(index=True)
    # What the group was granted on this resource -- distinct from a user's
    # standing within the group (UserAccessGroupLink.role). Postgres enum
    # resource_role, shared with that column; declared as str so
    # SQLite-backed tests can still create the table. Converted with
    # Role.from_db on read. read/write stay until phase 5 — see
    # docs/permission_system_plan.md.
    permission: str

    accessgroup: "AccessGroup" = Relationship(back_populates="acl")


class UserAccessGroupLink(SQLModel, table=True):
    __table_args__ = (
        Index(
            "one_owner_per_group",
            "accessgroup_id",
            unique=True,
            postgresql_where=text("role = 'owner'"),
            sqlite_where=text("role = 'owner'"),
        ),
    )

    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", primary_key=True
    )
    accessgroup_id: Optional[int] = Field(
        default=None, foreign_key="accessgroup.id", primary_key=True
    )
    # Standing in the group, not a grant. Postgres enum resource_role.
    role: str


class AccessGroup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_name: str = Field(index=True, unique=True)
    group_type: str = Field(default="custom", index=True)
    users: List["User"] = Relationship(back_populates="accessgroups", link_model=UserAccessGroupLink)
    acl: List["ResourceACL"] = Relationship(back_populates="accessgroup")


class UserProjectLink(SQLModel, table=True):
    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", primary_key=True
    )
    project_id: Optional[str] = Field(
        default=None, foreign_key="project.project_id", primary_key=True
    )


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    first_name: str
    last_name: str
    email: Optional[str]
    unique_id: str = Field(sa_column=Column("orcid", String, index=True, unique=True))
    is_service_account: Optional[bool] = Field(default=False)
    username: Optional[str] = Field(default=None, max_length=32, index=True)
    # Platform-wide bypass, deliberately not a group. Postgres enum platform_role.
    platform_role: Optional[str] = Field(default="none")
    projects: List["Project"] = Relationship(back_populates="users", link_model=UserProjectLink)
    accessgroups: List["AccessGroup"] = Relationship(back_populates="users", link_model=UserAccessGroupLink)


class ApiToken(SQLModel, table = True):
    access_group_id: str= Field(default = None)
    user_unique_id: str = Field(default = None, foreign_key = "user.orcid", index = True)
    token: str = Field(default = None, primary_key = True, index = True)
    token_created: str = Field(default_factory=get_utc_isoformat)


class HubTokenFamily(SQLModel, table=True):
    __table_args__ = (Index("ix_hubtokenfamily_expiry", "expires_at", "id"),)

    id: str = Field(primary_key=True, max_length=36)
    user_unique_id: str = Field(foreign_key="user.orcid", index=True)
    client_id: str = Field(index=True)
    created_at: datetime = Field(sa_type=DateTime(timezone=True))
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    last_refreshed_at: datetime = Field(sa_type=DateTime(timezone=True))
    revoked_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class HubRefreshToken(SQLModel, table=True):
    token_hash: str = Field(primary_key=True, max_length=64)
    family_id: str = Field(foreign_key="hubtokenfamily.id", index=True)
    generation: int
    issued_at: datetime = Field(sa_type=DateTime(timezone=True))
    spent_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class HubAccessToken(SQLModel, table=True):
    __table_args__ = (Index("ix_hubaccesstoken_expiry", "expires_at", "token_hash"),)

    token_hash: str = Field(primary_key=True, max_length=64)
    family_id: str = Field(foreign_key="hubtokenfamily.id", index=True)
    issued_at: datetime = Field(sa_type=DateTime(timezone=True))
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))


class HubLoginTransaction(SQLModel, table=True):
    __table_args__ = (Index("ix_hublogintransaction_expiry", "expires_at", "state_hash"),)

    state_hash: str = Field(primary_key=True, max_length=64)
    browser_secret_hash: str = Field(max_length=64)
    client_id: str
    callback_uri: str
    hub_state: str
    code_challenge: str = Field(max_length=43)
    nonce: str
    created_at: datetime = Field(sa_type=DateTime(timezone=True))
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    consumed_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class HubLoginCode(SQLModel, table=True):
    __table_args__ = (Index("ix_hublogincode_expiry", "expires_at", "code_hash"),)

    code_hash: str = Field(primary_key=True, max_length=64)
    user_unique_id: str = Field(foreign_key="user.orcid", index=True)
    client_id: str
    callback_uri: str
    code_challenge: str = Field(max_length=43)
    created_at: datetime = Field(sa_type=DateTime(timezone=True))
    expires_at: datetime = Field(sa_type=DateTime(timezone=True))
    consumed_at: Optional[datetime] = Field(default=None, sa_type=DateTime(timezone=True))


class Project(SQLModel, table=True):
    __table_args__ = (
        Index("ux_project_project_id_lower", func.lower(text("project_id")), unique=True),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    unique_id: Optional[str] = Field(default_factory=get_mfid_value, index=True, unique=True)
    project_id: str = Field(index=True, unique=True)
    organization: str
    status: Optional[str]
    title: Optional[str]
    project_lead_orcid: str = Field(foreign_key="user.orcid", index=True)
    project_lead_email: Optional[str] = None  # stored for old-API backward compat; not exposed in new response schemas
    users: List[User] = Relationship(back_populates="projects", link_model=UserProjectLink)
    resource: Optional["Resource"] = Relationship(
        sa_relationship_kwargs={"primaryjoin": "foreign(Resource.mfid)==Project.unique_id", "lazy": "noload"}
    )


# --- Resource / Identity ---

class ScientificMetadataBinary(SQLModel, table = True):
    __tablename__ = "scientific_metadata_new"
    id: Optional[int] = Field(default=None, primary_key=True)
    unique_id: str = Field(foreign_key="idtype.mfid", index=True)
    scientific_metadata: dict = Field(sa_column=Column(JSONB), default={})
    resource: Optional["Resource"] = Relationship(
        back_populates="scientific_metadata",
        sa_relationship_kwargs={"foreign_keys": "[ScientificMetadataBinary.unique_id]"}
    )
    
    __table_args__ = (
        Index(
            "ix_scientific_metadata_fts",
            func.to_tsvector(
                text("'english'"),
                func.jsonb_path_query_array(text('scientific_metadata'), text("'$[*]'")).cast(Text)
            ),
            postgresql_using="gin"
        ),
    )


class Resource(SQLModel, table=True):
    __tablename__ = "idtype"
    mfid: str = Field(default=None, primary_key=True, index=True, alias="unique_id")
    resource_type: str = Field(default=None, alias="object_type")
    creation_time: Optional[datetime] = Field(
        default_factory=get_utc_datetime,
        sa_column=Column(DateTime(timezone=True), nullable=True, server_default=func.now()),
    )
    modification_time: Optional[datetime] = Field(
        default_factory=get_utc_datetime,
        sa_column=Column(DateTime(timezone=True), nullable=True, server_default=func.now()),
    )
    public: bool = Field(default=False)
    created_with_api_version: Optional[str] = Field(default=None)
    api_version_githash: Optional[str] = Field(default=None)
   # last_modified_by_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.unique_id")
    scientific_metadata: Optional["ScientificMetadataBinary"] = Relationship(
            back_populates="resource", 
            sa_relationship_kwargs={"foreign_keys": "[ScientificMetadataBinary.unique_id]"})
    
    deletion_requests: List["DeletionRequest"] = Relationship(
        back_populates="resource",
        sa_relationship_kwargs={"foreign_keys": "[DeletionRequest.resource_id]"}
    )
    model_config = ConfigDict(populate_by_name=True)


class ResourceEmbedding(SQLModel, table=True):
    __tablename__ = "resource_embedding"
    __table_args__ = (
        Index(
            "ix_resource_embedding_embedding_cosine",
            "embedding",
            postgresql_using="ivfflat",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"lists": 100},
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    resource_mfid: str = Field(foreign_key="idtype.mfid", index=True)
    chunk_index: int
    chunk_text: str
    embedding: Any = Field(sa_column=Column(Vector(768)))
    model_name: str


class DeletionRequest(SQLModel, table=True):
    """Tracks a request to soft-delete a Dataset or Sample."""
    __table_args__ = (
        Index(
            "one_active_deletion_request_per_resource",
            "resource_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'approved')"),
            sqlite_where=text("status IN ('pending', 'approved')"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: str = Field(foreign_key="idtype.mfid", index=True)
    resource: Optional["Resource"] = Relationship(
        back_populates="deletion_requests",
        sa_relationship_kwargs={"foreign_keys": "[DeletionRequest.resource_id]"}
    )
    resource_name: Optional[str] = Field(default=None)
    requester_id: str = Field(index=True)
    reason: Optional[str] = Field(default=None)
    status: str = Field(default="pending", index=True)
    request_time: Optional[str] = Field(default_factory=get_utc_isoformat)
    review_time: Optional[str] = Field(default=None)
    reviewer_id: Optional[str] = Field(default=None)
    reviewer_notes: Optional[str] = Field(default=None)


class AccessGroupJoinRequest(SQLModel, table=True):
    """Tracks a request to join an access group. Currently only used for projects,
    but modeled generically since any access group could support this in the future."""
    __table_args__ = (
        Index(
            "one_pending_join_request_per_group_requester",
            "accessgroup_id",
            "requester_id",
            unique=True,
            postgresql_where=text("status = 'pending'"),
            sqlite_where=text("status = 'pending'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    accessgroup_id: int = Field(foreign_key="accessgroup.id", index=True)
    requester_id: str = Field(foreign_key="user.orcid", index=True)
    reason: Optional[str] = Field(default=None)
    status: str = Field(default="pending", index=True)
    request_time: Optional[str] = Field(default_factory=get_utc_isoformat)
    review_time: Optional[str] = Field(default=None)
    reviewer_id: Optional[str] = Field(default=None, foreign_key="user.orcid")
    reviewer_notes: Optional[str] = Field(default=None)


class DeletionAuditLog(SQLModel, table=True):
    """Permanent record of every approved hard-deletion. Survives after the resource is gone."""
    id: Optional[int] = Field(default=None, primary_key=True)
    resource_id: str = Field(index=True)       # no FK — the resource row will be deleted
    resource_type: Optional[str] = Field(default=None)
    resource_name: Optional[str] = Field(default=None)
    requester_id: str = Field(index=True)
    reason: Optional[str] = Field(default=None)
    request_time: Optional[str] = Field(default=None)
    reviewer_id: str = Field(index=True)
    reviewer_notes: Optional[str] = Field(default=None)
    review_time: Optional[str] = Field(default=None)
    deleted_at: str = Field(index=True)        # set by the API at deletion time
    project_id: Optional[str] = Field(default=None, index=True)
    project_mfid: Optional[str] = Field(default=None, index=True)


# --- Instrument ---

class Instrument(SQLModel, table=True):
    __table_args__ = (
        Index("ux_instrument_instrument_id_lower", func.lower(text("instrument_id")), unique=True),
    )

    id: int = Field(default=None, primary_key=True)
    unique_id: Optional[str] = Field(default_factory=get_mfid_value, index=True, unique=True)
    instrument_id: Optional[str] = Field(default=None, index=True, unique=True)
    other_id: Optional[str]
    other_id_source: Optional[str]
    instrument_name: str
    manufacturer: Optional[str]
    model: Optional[str]
    owner: str
    location: str
    description: Optional[str]
    instrument_type: Optional[str]
    # Instruments are not deleted; status tracks lifecycle. Changed through
    # POST /instruments/{id}/status, not PATCH — it is a lifecycle transition
    # with a higher bar than a metadata edit.
    status: Optional[str] = Field(default="active")


# --- Task tracking ---
class IngestionRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_mfid: Optional[str] = Field(default=None, foreign_key="dataset.unique_id", index=True, alias="dataset_id")
    file_mfid: Optional[str] = Field(default=None, foreign_key="associatedfile.mfid", index=True)
    file_to_upload: Optional[str] = None
    ingestion_githash: Optional[str] = None
    ingestion_class: Optional[str] = None
    time_created: Optional[str] = None
    time_completed: Optional[str] = None
    status: Optional[str] = None
    associated_file: Optional["AssociatedFile"] = Relationship(back_populates="ingestion_requests")


class UploadSession(SQLModel, table=True):
    __tablename__ = "upload_sessions"
    __table_args__ = (
        Index("ix_upload_sessions_work", "protocol", "next_attempt_at"),
        UniqueConstraint("owner_user_id", "dataset_mfid", "protocol", "idempotency_key", name="uq_upload_session_idempotency"),
        CheckConstraint("protocol IS NULL OR (protocol = 'gcs_xml_v1' AND state IS NOT NULL AND state IN ('initiating', 'uploading', 'finalizing', 'verifying', 'completed', 'aborting', 'aborted', 'expired', 'failed'))", name="ck_upload_session_protocol_state"),
        CheckConstraint("part_size IS NULL OR part_size > 0", name="ck_upload_session_part_size"),
        CheckConstraint("part_count IS NULL OR (part_count BETWEEN 1 AND 10000)", name="ck_upload_session_part_count"),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    upload_id: str = Field(default_factory=get_mfid_value, index=True, unique=True)
    dataset_mfid: str
    gcs_path: str           # full blob path in prod bucket: "{dsid}/{filename}"
    associated_file_name: str   # relative filename within the dataset: "{safe_filename}"
    owner_user_id: str
    expires_at: str         # written as ISO 8601 string; DB returns datetime, handle both
    size: Optional[int] = Field(default=None, sa_column=Column(BigInteger()))
    protocol: Optional[str] = None
    bucket: Optional[str] = None
    provider_upload_id: Optional[str] = None
    state: Optional[str] = None
    part_size: Optional[int] = Field(default=None, sa_column=Column(BigInteger()))
    part_count: Optional[int] = None
    idempotency_key: Optional[str] = None
    request_fingerprint: Optional[str] = None
    claimed_sha256: Optional[str] = None
    result_file_mfid: Optional[str] = None
    storage_generation: Optional[str] = None
    failure_code: Optional[str] = None
    lease_owner: Optional[str] = None
    version: Optional[int] = None
    part_manifest: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON().with_variant(JSONB(), "postgresql")))
    created_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    updated_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    lease_expires_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    cleanup_requested_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    claimed_crc32c: Optional[str] = None
    next_attempt_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    cleaned_at: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    part_signing_after: Optional[datetime] = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    attempts: Optional[int] = None
