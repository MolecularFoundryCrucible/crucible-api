from typing import Optional, List
from sqlalchemy import JSON, Column,BigInteger
from sqlmodel import Field, SQLModel,Relationship
from models.dataset import Dataset

class DatasetSampleLink(SQLModel, table=True):
    sample_id: Optional[int] = Field(
        default=None, foreign_key="sample.unique_id", primary_key=True, index=True
    )
    dataset_id: Optional[str] = Field(
        default=None, foreign_key="dataset.unique_id", primary_key=True
    )

class Sample(SQLModel, table = True):
    unique_id: str = Field(default = None, primary_key = True, index = True)
    owner_user_id: int = Field(default=None, index=True, foreign_key="user.id")
    date_created: Optional[str] = None
    description: Optional[str]
    datasets: List["Dataset"] = Relationship(back_populates="samples", link_model=DatasetSampleLink)

