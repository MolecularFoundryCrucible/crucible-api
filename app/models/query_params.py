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
ACCESSIBLE_TO_USER_DESCRIPTION = (
    "User references whose effective access must all include each result. "
    "Ordinary callers may select only themselves; platform contributors and "
    "administrators may select other users."
)
ACCESSIBLE_TO_PROJECT_DESCRIPTION = (
    "Project MFIDs or slugs whose direct access must all include each result. "
    "Ordinary callers must hold standing in each selected project; platform "
    "contributors and administrators may select any project."
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


class FacetSort(str, Enum):
    VALUE = "value"
    LABEL = "label"
    COUNT = "count"


class InstrumentStatus(str, Enum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


class DatasetFacetField(str, Enum):
    SESSION = "session"
    MEASUREMENT = "measurement"
    DATA_FORMAT = "data_format"
    OWNER = "owner"
    INSTRUMENT = "instrument"
    PROJECT = "project"


class SampleFacetField(str, Enum):
    SAMPLE_TYPE = "sample_type"
    OWNER = "owner"
    PROJECT = "project"


class StrictQueryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetDetailQuery(StrictQueryModel):
    include_metadata: bool = False
    include_links: bool = False
    include_owner: bool = True


class SampleDetailQuery(DatasetDetailQuery):
    include_datasets: bool = Field(
        default=True,
        description=(
            "Include legacy embedded dataset objects. Set false and use include_links or "
            "GET /datasets?sample_mfid=... for efficient relationship access."
        ),
    )


class ProjectDetailQuery(StrictQueryModel):
    include_metadata: bool = False
    include_members: bool = False


class InstrumentDetailQuery(StrictQueryModel):
    include_metadata: bool = False
    include_owner: bool = True


class ResourceDetailQuery(SampleDetailQuery):
    pass


class ResourceCollectionQuery(StrictQueryModel):
    accessible_to_user: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=ACCESSIBLE_TO_USER_DESCRIPTION,
    )
    accessible_to_project: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=ACCESSIBLE_TO_PROJECT_DESCRIPTION,
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
    anchor_mfid: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Start the page at this readable resource, inclusive, using the requested ordering.",
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
    session_name_is_null: Optional[bool] = Field(default=None, description="Filter by whether session_name is null.")
    measurement_is_null: Optional[bool] = Field(default=None, description="Filter by whether measurement is null.")
    data_format_is_null: Optional[bool] = Field(default=None, description="Filter by whether data_format is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Filter by whether the stored owner identifier is null.")
    instrument_mfid_is_null: Optional[bool] = Field(default=None, description="Filter by whether a canonical registered instrument is assigned.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Filter by whether a canonical primary project is assigned.")

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner_orcid = self.__dict__.get("owner_orcid")
        if self.owner_id is not None and owner_orcid is not None and self.owner_id != owner_orcid:
            raise ValueError("owner_id and owner_orcid must identify the same owner")
        if self.affiliation and (self.owner_id is not None or owner_orcid is not None):
            raise ValueError("affiliation cannot be combined with an exact owner filter")
        if self.session_name_is_null is True and self.session_name is not None:
            raise ValueError("session_name_is_null=true cannot be combined with session_name")
        if self.measurement_is_null is True and self.measurement is not None:
            raise ValueError("measurement_is_null=true cannot be combined with measurement")
        if self.data_format_is_null is True and self.data_format is not None:
            raise ValueError("data_format_is_null=true cannot be combined with data_format")
        if self.owner_id_is_null is True and (self.owner_id is not None or owner_orcid is not None):
            raise ValueError("owner_id_is_null=true cannot be combined with an exact owner filter")
        if self.instrument_mfid_is_null is True and self.instrument_mfid is not None:
            raise ValueError("instrument_mfid_is_null=true cannot be combined with instrument_mfid")
        if self.project_mfid_is_null is True and (self.project_id is not None or self.project_mfid is not None):
            raise ValueError("project_mfid_is_null=true cannot be combined with a project filter")
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
    sample_type_is_null: Optional[bool] = Field(default=None, description="Filter by whether sample_type is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Filter by whether the stored owner identifier is null.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Filter by whether a canonical primary project is assigned.")

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner_orcid = self.__dict__.get("owner_orcid")
        if self.owner_id is not None and owner_orcid is not None and self.owner_id != owner_orcid:
            raise ValueError("owner_id and owner_orcid must identify the same owner")
        if self.affiliation and (self.owner_id is not None or owner_orcid is not None):
            raise ValueError("affiliation cannot be combined with an exact owner filter")
        if self.sample_type_is_null is True and self.sample_type is not None:
            raise ValueError("sample_type_is_null=true cannot be combined with sample_type")
        if self.owner_id_is_null is True and (self.owner_id is not None or owner_orcid is not None):
            raise ValueError("owner_id_is_null=true cannot be combined with an exact owner filter")
        if self.project_mfid_is_null is True and (self.project_id is not None or self.project_mfid is not None):
            raise ValueError("project_mfid_is_null=true cannot be combined with a project filter")
        return self


class ResourceFacetQuery(StrictQueryModel):
    accessible_to_user: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=ACCESSIBLE_TO_USER_DESCRIPTION,
    )
    accessible_to_project: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=ACCESSIBLE_TO_PROJECT_DESCRIPTION,
    )
    affiliation: Optional[list[Affiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=AFFILIATION_DESCRIPTION,
    )
    visibility: Literal["all", "public", "private"] = Field(default="all", description="Restrict the resources contributing to bucket counts by public visibility.")
    project_id: Optional[str] = Field(default=None, description="Project slug. May be combined with a matching project_mfid.")
    project_mfid: Optional[str] = Field(default=None, description="Canonical project MFID. May be combined with a matching project_id.")
    project_scope: Optional[ProjectScope] = Field(default=None, description="Project relationship to include. Requires a project filter and defaults to assigned.")
    creation_time_gte: Optional[AwareDatetime] = Field(default=None, description="Inclusive creation-time lower bound.")
    creation_time_lte: Optional[AwareDatetime] = Field(default=None, description="Inclusive creation-time upper bound.")
    modification_time_gte: Optional[AwareDatetime] = Field(default=None, description="Inclusive modification-time lower bound.")
    modification_time_lte: Optional[AwareDatetime] = Field(default=None, description="Inclusive modification-time upper bound.")
    sort: FacetSort = Field(
        default=FacetSort.VALUE,
        description="Order buckets by raw value, canonical label, or count.",
    )
    direction: SortDirection = Field(default=SortDirection.ASC, description="Facet bucket ordering direction.")
    limit: int = Field(default=100, ge=1, le=1000, description="Maximum number of facet buckets to return.")
    cursor: Optional[str] = Field(default=None, min_length=1, max_length=4096, description="Opaque continuation cursor returned by a previous facet page.")

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

    def cursor_filters(self) -> dict:
        values = self.model_dump(mode="json", exclude={"cursor", "direction", "limit", "sort"})
        for field_name in (
            "creation_time_gte",
            "creation_time_lte",
            "modification_time_gte",
            "modification_time_lte",
        ):
            value = self.normalized_time(field_name)
            values[field_name] = value.isoformat() if value is not None else None
        return values


class DatasetFacetQuery(ResourceFacetQuery):
    field: DatasetFacetField = Field(description="Dataset field to group into facet buckets.")
    sample_mfid: Optional[str] = None
    instrument_mfid: Optional[str] = None
    session_name: Optional[str] = None
    measurement: Optional[str] = None
    data_format: Optional[str] = None
    owner_id: Optional[str] = Field(default=None, min_length=1)
    session_name_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether session_name is null.")
    measurement_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether measurement is null.")
    data_format_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether data_format is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether the stored owner identifier is null.")
    instrument_mfid_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by canonical instrument assignment.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by canonical primary project assignment.")

    @model_validator(mode="after")
    def validate_facet_filters(self):
        if self.affiliation and self.owner_id is not None:
            raise ValueError("affiliation cannot be combined with owner_id")
        if self.session_name_is_null is True and self.session_name is not None:
            raise ValueError("session_name_is_null=true cannot be combined with session_name")
        if self.measurement_is_null is True and self.measurement is not None:
            raise ValueError("measurement_is_null=true cannot be combined with measurement")
        if self.data_format_is_null is True and self.data_format is not None:
            raise ValueError("data_format_is_null=true cannot be combined with data_format")
        if self.owner_id_is_null is True and self.owner_id is not None:
            raise ValueError("owner_id_is_null=true cannot be combined with owner_id")
        if self.instrument_mfid_is_null is True and self.instrument_mfid is not None:
            raise ValueError("instrument_mfid_is_null=true cannot be combined with instrument_mfid")
        if self.project_mfid_is_null is True and (self.project_id is not None or self.project_mfid is not None):
            raise ValueError("project_mfid_is_null=true cannot be combined with a project filter")
        return self


class SampleFacetQuery(ResourceFacetQuery):
    field: SampleFacetField = Field(description="Sample field to group into facet buckets.")
    dataset_mfid: Optional[str] = None
    sample_type: Optional[str] = None
    owner_id: Optional[str] = Field(default=None, min_length=1)
    sample_type_is_null: Optional[bool] = Field(default=None, description="Restrict contributing samples by whether sample_type is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Restrict contributing samples by whether the stored owner identifier is null.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Restrict contributing samples by canonical primary project assignment.")

    @model_validator(mode="after")
    def validate_facet_filters(self):
        if self.affiliation and self.owner_id is not None:
            raise ValueError("affiliation cannot be combined with owner_id")
        if self.sample_type_is_null is True and self.sample_type is not None:
            raise ValueError("sample_type_is_null=true cannot be combined with sample_type")
        if self.owner_id_is_null is True and self.owner_id is not None:
            raise ValueError("owner_id_is_null=true cannot be combined with owner_id")
        if self.project_mfid_is_null is True and (self.project_id is not None or self.project_mfid is not None):
            raise ValueError("project_mfid_is_null=true cannot be combined with a project filter")
        return self


class ProjectListQuery(StrictQueryModel):
    project_id: Optional[str] = Field(default=None, description="Exact project slug.")
    accessible_to_user: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=ACCESSIBLE_TO_USER_DESCRIPTION,
    )
    accessible_to_project: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=ACCESSIBLE_TO_PROJECT_DESCRIPTION,
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
    cursor: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Opaque continuation cursor returned by a previous page.",
    )
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

    def normalized_result_filters(self) -> dict:
        return self.model_dump(
            mode="json",
            exclude={
                "cursor", "offset", "limit", "include_metadata",
                "include_members", "include_total",
            },
        )


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
    cursor: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Opaque continuation cursor returned by a previous page.",
    )
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

    def normalized_result_filters(self) -> dict:
        values = self.model_dump(
            mode="json",
            exclude={
                "cursor", "offset", "limit", "include_metadata",
                "include_owner", "include_total", "owner",
            },
        )
        values["owner_id"] = self.owner_id or self.__dict__.get("owner")
        if self.instrument_id is None and self.status is None:
            values["status"] = InstrumentStatus.ACTIVE.value
        return values


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
