from typing import Optional, List
from sqlalchemy import JSON, Column,BigInteger
from sqlmodel import Field, SQLModel,Relationship
from mfid import mfid


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


class DatasetCollectionLink(SQLModel, table=True):
    collection_id: Optional[int] = Field(
        default=None, foreign_key="datasetcollection.id", primary_key=True, index=True
    )
    dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True
    )


class DatasetCollection(SQLModel, table = True):
    id: Optional[int] = Field(default=None, primary_key=True)
    unique_id: str = Field(default_factory = get_mfid_value, index = True, unique = True)
    other_id: Optional[str] = None
    date_created: Optional[str] = None
    description: Optional[str]
    datasets: List["Dataset"] = Relationship(back_populates="collections", link_model=DatasetCollectionLink)


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


class Sample(SQLModel, table = True):
    id: Optional[int] = Field(default = None, primary_key = True)
    unique_id: str = Field(default_factory= get_mfid_value, index = True, unique = True)
    sample_name: str = Field(index = True, default = None) 
    owner_orcid: Optional[str]
    owner_user_id: Optional[int] = Field(default=None, index=True, foreign_key="user.id")
    date_created: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str]
    datasets: List["Dataset"] = Relationship(back_populates="samples", link_model=DatasetSampleLink)
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

    class Config:
        arbitrary_types_allowed = True
        from_attributes = True


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
    source_folder: Optional[str] # folder where the data was synced from (on instrument PC)
    instrument_name: Optional[str]
    creation_time: Optional[str]
    size: Optional[int] = Field(sa_column=Column(BigInteger())) 
    data_format: Optional[str]
    measurement: Optional[str]
    session_name: Optional[str]
    json_link: Optional[str] 
    sha256_hash_file_to_upload: Optional[str]
    scientific_metadata: Optional[dict] = Relationship(back_populates = 'dataset')
    samples: List[Sample] = Relationship(back_populates="datasets", link_model = DatasetSampleLink)
    collections: List[DatasetCollection] = Relationship(back_populates = "datasets", link_model = DatasetCollectionLink)
    keywords: List[Keyword] = Relationship(back_populates="datasets", link_model=DatasetKeywordLink)
    thumbnails: List[Thumbnail] = Relationship(back_populates = "dataset")
    associated_files: List[AssociatedFile] = Relationship(back_populates = "dataset")
    acl: List["DatasetACL"] = Relationship(back_populates="dataset")
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

    class Config:
        arbitrary_types_allowed = True
        from_attributes = True


class DatasetACL(SQLModel, table=True):
    accessgroup_id: Optional[int] = Field(
        default=None, foreign_key="accessgroup.id", primary_key=True
    )
    dataset_id: Optional[int] = Field(
        default=None, foreign_key="dataset.id", primary_key=True
    )
    read:  bool = Field(index=True)
    write: bool = Field(index=True)

    dataset: Dataset = Relationship(back_populates="acl")
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
    #datasets: List[Dataset] = Relationship(back_populates="accessgroups", link_model=DatasetAccessGroupLink)
    acl: List[DatasetACL] = Relationship(back_populates="accessgroup")


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
    lbl_email: Optional[str]
    orcid: str = Field(index=True, unique=True)
    employee_number: Optional[str]
    projects: List["Project"] = Relationship(back_populates="users", link_model=UserProjectLink)
    accessgroups: List["AccessGroup"] = Relationship(back_populates="users", link_model=UserAccessGroupLink)


class Project(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: str = Field(index=True, unique=True)
    organization: str 
    status: Optional[str]
    title: Optional[str]
    project_lead_name: Optional[str]
    project_lead_email: str
    users: List[User] = Relationship(back_populates="projects", link_model=UserProjectLink)


class ScientificMetadata(SQLModel, table = True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_unique_id: str
    scientific_metadata: dict = Field(sa_column=Column(JSON), default={})






