from typing import Optional, List
from pydantic import ConfigDict
from sqlalchemy import JSON, Column, BigInteger, func, Index, text, Text, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlmodel import Field, SQLModel,Relationship, Column
from mfid import mfid
from app.general_utils import get_tz_isoformat


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
    dataset_id: int = Field(foreign_key = "dataset.id")
    dataset: "Dataset" = Relationship(back_populates="thumbnails")


class AssociatedFile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    filename: str
    size: int = Field(sa_column=Column(BigInteger())) 
    sha256_hash: str
    dataset_id: int = Field(foreign_key = "dataset.id")
    dataset: "Dataset" = Relationship(back_populates = 'associated_files')


class DatasetLink(SQLModel, table = True):
    child_dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True, index=True
    )
    parent_dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True, index = True
    )


class DatasetSampleLink(SQLModel, table=True):
    sample_id: Optional[int] = Field(
        default=None, foreign_key="sample.id", primary_key=True, index=True
    )
    dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True
    )


class SampleLink(SQLModel, table=True):
    sample_id: Optional[int] = Field(
        default=None, foreign_key="sample.id", primary_key=True, index=True
    )
    parent_sample_id: Optional[int] = Field(
        default=None, foreign_key="sample.id", primary_key=True
    )

# date_created >> timestamp : this is the user defined date when the sample was created (can be null)
class Sample(SQLModel, table = True):
    id: Optional[int] = Field(default = None, primary_key = True)
    unique_id: str = Field(default_factory= get_mfid_value, index = True, unique = True)
    sample_name: str = Field(index = True, default = None) 
    sample_type: Optional[str] = Field(index = True, default = None)
    owner_orcid: Optional[str]
    owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
    timestamp: Optional[str] = Field(default_factory=get_tz_isoformat,
                                     sa_column=Column("timestamp", String, nullable=True),
                                     alias="date_created")
    project_id: Optional[str] = None
    description: Optional[str]
    datasets: List["Dataset"] = Relationship(back_populates="samples", link_model=DatasetSampleLink)
    resource: Optional["Resource"] = Relationship(
        sa_relationship_kwargs={
            "primaryjoin": "foreign(Resource.mfid)==Sample.unique_id",
            "lazy": "noload",
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
    file_to_upload: Optional[str] = Field(default = None)  
    owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
    owner_orcid: Optional[str] = Field(default = None)
    project_id: Optional[str] = Field(default=None, index=True, foreign_key="project.project_id")
    instrument_id: Optional[int]  = Field(default = None, index = True, foreign_key = "instrument.id")
    source_folder: Optional[str] 
    instrument_name: Optional[str]
    timestamp: Optional[str] = Field(default_factory=get_tz_isoformat, sa_column=Column("timestamp", String, nullable=True))
    size: Optional[int] = Field(sa_column=Column(BigInteger())) 
    data_format: Optional[str]
    measurement: Optional[str]
    data_type: Optional[str]
    session_name: Optional[str]
    ingestion_githash: Optional[str] = None
    ingestion_class: Optional[str] = None
    sha256_hash_file_to_upload: Optional[str]
    scientific_metadata: Optional["ScientificMetadata"] = Relationship(
        back_populates = 'dataset')
    scientific_metadata_new: Optional["NewScientificMetadata"] = Relationship(
        back_populates="dataset", 
        sa_relationship_kwargs={"foreign_keys": "[NewScientificMetadata.dataset_id]"})
    samples: List[Sample] = Relationship(back_populates="datasets", link_model = DatasetSampleLink)
    keywords: List[Keyword] = Relationship(back_populates="datasets", link_model=DatasetKeywordLink)
    thumbnails: List[Thumbnail] = Relationship(back_populates = "dataset")
    associated_files: List[AssociatedFile] = Relationship(back_populates = "dataset")
    resource: Optional["Resource"] = Relationship(sa_relationship_kwargs={"primaryjoin": "foreign(Resource.mfid)==Dataset.unique_id", "lazy": "noload",})
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
    mfid: str = Field(foreign_key="idtype.mfid", primary_key=True, index=True)
    accessgroup_id: Optional[int] = Field(
        default=None, foreign_key="accessgroup.id", primary_key=True
    )
    read: bool = Field(index=True)
    write: bool = Field(index=True)

    accessgroup: "AccessGroup" = Relationship(back_populates="acl")


class UserAccessGroupLink(SQLModel, table=True):
    user_id: Optional[int] = Field(
        default=None, foreign_key="user.id", primary_key=True
    )
    accessgroup_id: Optional[int] = Field(
        default=None, foreign_key="accessgroup.id", primary_key=True
    )


class AccessGroup(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    group_name: str = Field(index=True, unique=True)
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
    is_service_account: bool = Field(default=False)
    projects: List["Project"] = Relationship(back_populates="users", link_model=UserProjectLink)
    accessgroups: List["AccessGroup"] = Relationship(back_populates="users", link_model=UserAccessGroupLink)


class ApiToken(SQLModel, table = True):
    user_unique_id: str = Field(default = None, foreign_key = "user.orcid", index = True)
    token: str = Field(default = None, primary_key = True, index = True)
    token_created: str = Field(default_factory=get_tz_isoformat)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: str = Field(index=True, unique=True)
    organization: str
    status: Optional[str]
    title: Optional[str]
    project_lead_orcid: str = Field(index=True)
    project_lead_email: Optional[str] = None  # stored for old-API backward compat; not exposed in new response schemas
    users: List[User] = Relationship(back_populates="projects", link_model=UserProjectLink)


class ScientificMetadata(SQLModel, table = True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_unique_id: str = Field(foreign_key="dataset.unique_id", index=True)
    scientific_metadata: dict = Field(sa_column=Column(JSON), default={})
    dataset: Optional["Dataset"] = Relationship(back_populates="scientific_metadata")


#New Scientific Metadata table for FTS (Full-Text Search)
class NewScientificMetadata(SQLModel, table = True):
    __tablename__ = "scientific_metadata_new"
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_mfid: str = Field(foreign_key="dataset.unique_id", index=True)
    dataset_id: Optional[int] = Field(foreign_key="dataset.id", index=True)
    scientific_metadata: dict = Field(sa_column=Column(JSONB), default={})
    dataset: Optional["Dataset"] = Relationship(
        back_populates="scientific_metadata_new",
        sa_relationship_kwargs={"foreign_keys": "[NewScientificMetadata.dataset_id]"}
    )
    
    __table_args__ = (
        Index(
            "ix_scientific_metadata_new_fts",
            func.to_tsvector(
                text("'english'"),
                func.jsonb_path_query_array(text('scientific_metadata'), text("'$[*]'")).cast(Text)
            ),
            postgresql_using="gin"
        ),
    )


# --- Resource / Identity ---

class Resource(SQLModel, table=True):
    __tablename__ = "idtype"
    mfid: str = Field(default=None, primary_key=True, index=True, alias="unique_id")
    resource_type: str = Field(default=None, alias="object_type")
    creation_time: Optional[str] = Field(default_factory=get_tz_isoformat)
    modification_time: Optional[str] = Field(default_factory=get_tz_isoformat)
    created_with_api_version: Optional[str] = Field(default=None)
    api_version_githash: Optional[str] = Field(default=None)
   # last_modified_by_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.unique_id")
    deletion_requests: List["DeletionRequest"] = Relationship(
        back_populates="resource",
        sa_relationship_kwargs={"foreign_keys": "[DeletionRequest.resource_id]"}
    )
    model_config = ConfigDict(populate_by_name=True)


class DeletionRequest(SQLModel, table=True):
    """Tracks a request to soft-delete a Dataset or Sample."""
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
    request_time: Optional[str] = Field(default_factory=get_tz_isoformat)
    review_time: Optional[str] = Field(default=None)
    reviewer_id: Optional[str] = Field(default=None)
    reviewer_notes: Optional[str] = Field(default=None)


# --- Instrument ---

class Instrument(SQLModel, table=True):
    id: int = Field(default=None, primary_key=True)
    unique_id: Optional[str] = Field(default_factory=get_mfid_value)
    other_id: Optional[str]
    other_id_source: Optional[str]
    instrument_name: str
    manufacturer: Optional[str]
    model: Optional[str]
    owner: str
    location: str
    description: Optional[str]
    instrument_type: Optional[str]


# --- Task tracking ---
class IngestionRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(default=None, index=True)
    file_to_upload: str = None
    time_created: Optional[str] = None
    time_completed: Optional[str] = None
    status: Optional[str] = None

