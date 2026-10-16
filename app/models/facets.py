from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.instrument import InstrumentReferenceRead
from app.models.projects import ProjectReferenceRead
from app.models.users import UserPublicRead


class FacetBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Optional[str]
    label: Optional[str] = Field(
        default=None,
        description="Canonical display label. Null only for the null bucket.",
    )
    count: int


class OwnerFacetBucket(FacetBucket):
    owner: Optional[UserPublicRead]


class ProjectFacetBucket(FacetBucket):
    project: Optional[ProjectReferenceRead]


class InstrumentFacetBucket(FacetBucket):
    instrument: Optional[InstrumentReferenceRead]


class RequesterFacetBucket(FacetBucket):
    requester: Optional[UserPublicRead]


FacetBucketResponse = (
    FacetBucket | OwnerFacetBucket | ProjectFacetBucket | InstrumentFacetBucket | RequesterFacetBucket
)


class FacetResponse(BaseModel):
    field: str
    limit: int
    items: list[FacetBucketResponse]
    next_cursor: Optional[str] = None
