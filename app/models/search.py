from typing import Any, Optional
from sqlmodel import SQLModel

from app.models.timestamps import UtcDatetime


class ResourceSearchResult(SQLModel):
    '''Unified search result compatible across all search endpoints.
    scientific_metadata is populated by GET /resources/metadata/search; null on name-based endpoints.
    '''
    unique_id: str
    resource_type: Optional[str] = None
    name: Optional[str] = None
    owner_orcid: Optional[str] = None
    creation_time: Optional[UtcDatetime] = None
    modification_time: Optional[UtcDatetime] = None
    rank: Optional[float] = None
    scientific_metadata: Optional[Any] = None
