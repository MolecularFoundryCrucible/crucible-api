from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Optional

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models.projects import ProjectScope


AFFILIATION_DESCRIPTION = (
    "Relationship between the authenticated caller and each result. "
    "Repeated values use OR semantics."
)


class Affiliation(str, Enum):
    OWNER = "owner"


class ResourceSort(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    NAME = "name"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class InstrumentStatus(str, Enum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


class StrictQueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResourceCollectionQuery(StrictQueryModel):
    accessible_to_user: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="User references whose effective access must all include each result.",
    )
    accessible_to_project: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="Project MFIDs or slugs whose direct access must all include each result.",
    )
    affiliation: Optional[list[Affiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=AFFILIATION_DESCRIPTION,
    )
    include_metadata: bool = False
    include_links: bool = False
    include_owner: bool = False
    include_total: Optional[bool] = Field(
        default=None,
        description=(
            "Exact authorization-filtered total. Omit to preserve legacy endpoint behavior; "
            "true always computes it and false always omits it."
        ),
    )
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Opaque continuation cursor returned by a previous page.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Compatibility offset. A supplied cursor takes precedence.",
    )
    creation_time_gte: Optional[AwareDatetime] = Field(default=None, description="Inclusive creation-time lower bound.")
    creation_time_lte: Optional[AwareDatetime] = Field(default=None, description="Inclusive creation-time upper bound.")
    modification_time_gte: Optional[AwareDatetime] = Field(default=None, description="Inclusive modification-time lower bound.")
    modification_time_lte: Optional[AwareDatetime] = Field(default=None, description="Inclusive modification-time upper bound.")
    visibility: Literal["all", "public", "private"] = Field(
        default="all",
        description="Restrict results by public visibility.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Project slug. May be combined with a matching project_mfid.",
    )
    project_mfid: Optional[str] = Field(
        default=None,
        description="Canonical project MFID. May be combined with a matching project_id.",
    )
    project_scope: Optional[ProjectScope] = Field(
        default=None,
        description="Project relationship to include. Requires project_id or project_mfid and defaults to assigned.",
    )
    sort: Optional[ResourceSort] = Field(
        default=None,
        description="Explicit ordering. Omit to preserve the legacy unique_id descending order.",
    )
    direction: SortDirection = Field(
        default=SortDirection.DESC,
        description="Direction for an explicit sort. Supplying direction without sort returns 422.",
    )

    @field_validator("accessible_to_user", "accessible_to_project")
    @classmethod
    def validate_access_selectors(cls, values):
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("access selectors cannot contain blank values")
        return normalized

    @model_validator(mode="after")
    def validate_combinations(self):
        if self.project_scope is not None and self.project_id is None and self.project_mfid is None:
            raise ValueError("project_scope requires project_id or project_mfid")
        if self.creation_time_gte and self.creation_time_lte and self.creation_time_gte > self.creation_time_lte:
            raise ValueError("creation_time_gte cannot be after creation_time_lte")
        if self.modification_time_gte and self.modification_time_lte and self.modification_time_gte > self.modification_time_lte:
            raise ValueError("modification_time_gte cannot be after modification_time_lte")
        return self

    def normalized_time(self, field_name: str) -> Optional[datetime]:
        value: Optional[datetime] = getattr(self, field_name)
        return value.astimezone(timezone.utc) if value is not None else None

    def normalized_result_filters(self, exclude: set[str]) -> dict:
        values = self.model_dump(mode="json", exclude=exclude)
        for field_name in (
            "creation_time_gte",
            "creation_time_lte",
            "modification_time_gte",
            "modification_time_lte",
        ):
            value = self.normalized_time(field_name)
            values[field_name] = value.isoformat() if value is not None else None
        return values


class DatasetListQuery(ResourceCollectionQuery):
    sample_mfid: Optional[str] = Field(
        default=None,
        description="Filter datasets linked to this canonical sample MFID.",
    )
    instrument_mfid: Optional[str] = Field(
        default=None,
        description="Filter datasets assigned to this canonical instrument MFID.",
    )
    keyword: Optional[str] = None
    unique_id: Optional[str] = None
    public: Optional[bool] = None
    dataset_name: Optional[str] = None
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact stable owner identifier for a human or service account.",
    )
    owner_orcid: Annotated[Optional[str], Field(
        deprecated=True,
        description="Deprecated exact owner filter. Use owner_id, or affiliation=owner for the authenticated caller.",
    )] = None
    instrument_name: Optional[str] = None
    timestamp: Optional[str] = None
    size: Optional[int] = None
    data_format: Optional[str] = None
    data_type: Optional[str] = None
    measurement: Optional[str] = None
    session_name: Optional[str] = None

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner_orcid = self.__dict__.get("owner_orcid")
        if self.owner_id is not None and owner_orcid is not None and self.owner_id != owner_orcid:
            raise ValueError("owner_id and owner_orcid must identify the same owner")
        if self.affiliation and (self.owner_id is not None or owner_orcid is not None):
            raise ValueError("affiliation cannot be combined with an exact owner filter")
        return self


class SampleListQuery(ResourceCollectionQuery):
    include_datasets: Annotated[bool, Field(
        deprecated=True,
        description="Deprecated embedded dataset expansion. Use include_links instead.",
    )] = False
    dataset_mfid: Optional[str] = Field(
        default=None,
        description="Filter samples linked to this canonical dataset MFID.",
    )
    unique_id: Optional[str] = None
    public: Optional[bool] = None
    sample_name: Optional[str] = None
    sample_type: Optional[str] = None
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact stable owner identifier for a human or service account.",
    )
    owner_orcid: Annotated[Optional[str], Field(
        deprecated=True,
        description="Deprecated exact owner filter. Use owner_id, or affiliation=owner for the authenticated caller.",
    )] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner_orcid = self.__dict__.get("owner_orcid")
        if self.owner_id is not None and owner_orcid is not None and self.owner_id != owner_orcid:
            raise ValueError("owner_id and owner_orcid must identify the same owner")
        if self.affiliation and (self.owner_id is not None or owner_orcid is not None):
            raise ValueError("affiliation cannot be combined with an exact owner filter")
        return self


class ProjectListQuery(StrictQueryModel):
    project_id: Optional[str] = Field(default=None, description="Exact project slug.")
    accessible_to_user: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="User references whose effective access must all include each result.",
    )
    accessible_to_project: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description="Project MFIDs or slugs whose direct access must all include each result.",
    )
    affiliation: Optional[list[Affiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=AFFILIATION_DESCRIPTION,
    )
    include_metadata: bool = False
    include_total: Optional[bool] = Field(
        default=None,
        description=(
            "Exact authorization-filtered total. Omit to preserve legacy endpoint behavior; "
            "true always computes it and false always omits it."
        ),
    )
    include_members: bool = Field(
        default=False,
        description="Include members for an exact project_id lookup when authorized.",
    )
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)

    @field_validator("accessible_to_user", "accessible_to_project")
    @classmethod
    def validate_access_selectors(cls, values):
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("access selectors cannot contain blank values")
        return normalized

    @model_validator(mode="after")
    def validate_combinations(self):
        if self.include_members and self.project_id is None:
            raise ValueError("include_members requires an exact project_id filter")
        return self


class InstrumentListQuery(StrictQueryModel):
    instrument_id: Optional[str] = Field(default=None, description="Exact instrument slug.")
    affiliation: Optional[list[Affiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=AFFILIATION_DESCRIPTION,
    )
    include_metadata: bool = False
    include_owner: bool = False
    include_total: Optional[bool] = Field(
        default=None,
        description=(
            "Exact authorization-filtered total. Omit to preserve legacy endpoint behavior; "
            "true always computes it and false always omits it."
        ),
    )
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)
    other_id: Optional[str] = None
    other_id_source: Optional[str] = None
    instrument_name: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact stable owner identifier for a human or service account.",
    )
    owner: Annotated[Optional[str], Field(
        deprecated=True,
        description="Deprecated exact owner filter. Use owner_id, or affiliation=owner for the authenticated caller.",
    )] = None
    location: Optional[str] = None
    description: Optional[str] = None
    instrument_type: Optional[str] = None
    status: Optional[InstrumentStatus] = Field(
        default=None,
        description="Restrict results to one lifecycle status.",
    )

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner = self.__dict__.get("owner")
        if self.owner_id is not None and owner is not None and self.owner_id != owner:
            raise ValueError("owner_id and owner must identify the same owner")
        if self.affiliation and (self.owner_id is not None or owner is not None):
            raise ValueError("affiliation cannot be combined with an exact owner filter")
        return self


class SearchQuery(StrictQueryModel):
    q: str = Field(
        min_length=3,
        max_length=200,
        description="Search text. Whitespace is normalized and at most 10 terms are accepted.",
    )
    affiliation: Optional[list[Affiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=AFFILIATION_DESCRIPTION,
    )
    limit: int = Field(default=20, ge=1, le=50)


class ResourceSearchQuery(SearchQuery):
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact stable owner identifier for a human or service account.",
    )
    project_id: Optional[str] = Field(
        default=None,
        description="Restrict results related to this project slug. May be combined with a matching project_mfid.",
    )
    project_mfid: Optional[str] = Field(
        default=None,
        description="Restrict results related to this canonical project MFID.",
    )
    project_scope: Optional[ProjectScope] = Field(
        default=None,
        description="Project relationship to include. Requires project_id or project_mfid and defaults to assigned.",
    )

    @model_validator(mode="after")
    def validate_project_scope(self):
        if self.project_scope is not None and self.project_id is None and self.project_mfid is None:
            raise ValueError("project_scope requires project_id or project_mfid")
        if self.affiliation and self.owner_id is not None:
            raise ValueError("affiliation cannot be combined with owner_id")
        return self


class InstrumentSearchQuery(SearchQuery):
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact stable owner identifier for a human or service account.",
    )
    status: Optional[InstrumentStatus] = Field(
        default=None,
        description="Restrict results to one lifecycle status.",
    )
    include_owner: bool = False

    @model_validator(mode="after")
    def validate_affiliation(self):
        if self.affiliation and self.owner_id is not None:
            raise ValueError("affiliation cannot be combined with owner_id")
        return self
