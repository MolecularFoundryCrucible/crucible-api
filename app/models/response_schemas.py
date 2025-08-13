from pydantic import BaseModel
from typing import List, Optional

class DatasetResponse(BaseModel):
    id: int
    unique_id: str
    dataset_name: Optional[str]

    class Config:
        from_attributes = True  
        
class SampleWithDatasetsResponse(BaseModel):
    id: int
    unique_id: str
    sample_name: str 
    owner_orcid: Optional[str]
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
    
    class Config:
        from_attributes = True