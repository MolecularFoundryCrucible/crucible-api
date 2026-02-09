from pydantic import BaseModel
from typing import List, Optional

class DatasetResponse(BaseModel):
    id: int
    unique_id: str
    dataset_name: Optional[str]
    measurement: Optional[str]
    class Config:
        from_attributes = True  
    
class SampleWithDatasetsResponse(BaseModel):
    id: int
    unique_id: str
    sample_name: str
    sample_type: Optional[str]
    owner_orcid: Optional[str]
    project_id: Optional[str]
    date_created: Optional[str] = None
    description: Optional[str]
    datasets: List[DatasetResponse] = []
    
    class Config:
        from_attributes = True

class DatasetSampleLinkResponse(BaseModel):
    dataset_id: str
    dataset_name: str
    sample_id: str
    sample_name: str
    sample_type: Optional[str]
    class Config:
        from_attributes = True


class AccountInfo(BaseModel):
    access_group_name: str
    access_group_ids: List[int]
    user_info: dict