from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional

from app.models.users import UserRead

class DatasetResponse(BaseModel):
    id: int
    unique_id: str
    dataset_name: Optional[str]
    measurement: Optional[str]
    timestamp: Optional[str] = None

    class Config:
        from_attributes = True
    
class SampleWithDatasetsResponse(BaseModel):
    id: int
    unique_id: str
    sample_name: str
    sample_type: Optional[str]
    owner_orcid: Optional[str]
    project_id: Optional[str]
    date_created: Optional[str] = Field(default=None, validation_alias="timestamp")
    timestamp: Optional[str] = None
    description: Optional[str]
    datasets: List[DatasetResponse] = []
    
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)
    

class DatasetSampleLinkResponse(BaseModel):
    dataset_id: str
    dataset_name: str
    sample_id: str
    sample_name: str
    sample_type: Optional[str]
    class Config:
        from_attributes = True


class AccountInfo(BaseModel):
    user_unique_id: str
    access_group_ids: List[int]
    user_info: Optional[UserRead] = None


class ApiKeyRead(BaseModel):
    api_key: str


class ApiKeyVerify(BaseModel):
    valid: bool
    created_at: str
    expires_at: str