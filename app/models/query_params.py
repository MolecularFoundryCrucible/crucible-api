from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Literal, Optional

from app.models.timestamps import UtcDatetime
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.models.projects import ProjectScope
from app.models.resource_graph import (
    GraphDirection,
    GraphRelationshipKind,
    GraphRelationshipTypeFilter,
)
from app.utils.permissions import PlatformRole
from app.models.access import AccessPermission


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


class ResourceAffiliation(str, Enum):
    OWNER = "owner"
    MEMBER = "member"


RESOURCE_AFFILIATION_DESCRIPTION = (
    AFFILIATION_DESCRIPTION
    + " owner matches records the caller owns. member matches records the caller reaches through"
    " one of their own grants (personal, project, instrument, or group), so records visible only"
    " because they are public are excluded; combine with visibility to narrow further. For platform"
    " administrators, member counts only their own grants, not their global access."
)


class InstrumentAffiliation(str, Enum):
    OWNER = "owner"
    MAINTAINER = "maintainer"


INSTRUMENT_AFFILIATION_DESCRIPTION = (
    AFFILIATION_DESCRIPTION
    + " maintainer matches instruments where the caller holds editor or admin on the instrument record."
)
MEMBER_REF_DESCRIPTION = (
    "Projects where every listed user is a member. Accepts an ORCID, service-account MFID, "
    "username, or email. Repeated values use AND semantics, like accessible_to_user. "
    "Platform contributors and administrators may select another user; other callers only themselves."
)
SORT_DIRECTION_DESCRIPTION = (
    "Direction for an explicit sort. Defaults to desc for created, updated, activity, and last_active, asc otherwise. "
    "Supplying direction without sort returns 422."
)
PROJECT_SORT_DESCRIPTION = (
    "Explicit ordering with unique_id as tie-breaker and nulls last. title is case-insensitive; "
    "project_id uses the stored slug, matching the default order. activity is the last change to the "
    "project's datasets, samples, files, or membership, recorded at most hourly. Omit to keep the default "
    "project_id ascending order."
)
ACTIVE_SINCE_DESCRIPTION = "Only records with activity at or after this time. Activity is recorded at most hourly."
INSTRUMENT_SORT_DESCRIPTION = (
    "Explicit ordering with unique_id as tie-breaker and nulls last. instrument_id and name "
    "(instrument_name) are case-insensitive. activity is the last time a dataset recorded on the "
    "instrument changed, recorded at most hourly. Omit to keep the default newest-first order."
)
INSTRUMENT_MEMBERS_DESCRIPTION = (
    "Include people and projects holding a role on the instrument record. "
    "Returned only to callers who hold a role on the instrument and to platform administrators."
)


class ResourceSort(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    NAME = "name"


class SortDirection(str, Enum):
    ASC = "asc"
    DESC = "desc"


class ProjectSort(str, Enum):
    PROJECT_ID = "project_id"
    TITLE = "title"
    CREATED = "created"
    UPDATED = "updated"
    ACTIVITY = "activity"


class InstrumentSort(str, Enum):
    INSTRUMENT_ID = "instrument_id"
    NAME = "name"
    CREATED = "created"
    UPDATED = "updated"
    ACTIVITY = "activity"


class FacetSort(str, Enum):
    VALUE = "value"
    LABEL = "label"
    COUNT = "count"


class InstrumentStatus(str, Enum):
    ACTIVE = "active"
    MAINTENANCE = "maintenance"
    DECOMMISSIONED = "decommissioned"


class UserSearchField(str, Enum):
    FIRST_NAME = "first_name"
    LAST_NAME = "last_name"
    USERNAME = "username"
    UNIQUE_ID = "unique_id"
    EMAIL = "email"


class UserSort(str, Enum):
    NAME = "name"
    USERNAME = "username"
    UNIQUE_ID = "unique_id"
    EMAIL = "email"
    LAST_ACTIVE = "last_active"


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


MFID_FILTER_MAX = 100
UNIQUE_ID_FILTER_DEPRECATION = "@1792540800"
MFID_FILTER_DESCRIPTION = (
    f"Repeatable exact MFID filter, at most {MFID_FILTER_MAX} values, OR semantics. "
    "MFIDs the caller cannot read, that do not exist, or that are pending deletion are omitted."
)


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
    include_members: bool = Field(default=False, description=INSTRUMENT_MEMBERS_DESCRIPTION)


class ResourceDetailQuery(SampleDetailQuery):
    pass


class ResourceEmbeddingListQuery(StrictQueryModel):
    id: Optional[int] = Field(default=None, ge=1, description="Exact embedding row ID.")
    resource_mfid: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact resource MFID.",
    )
    chunk_index: Optional[int] = Field(default=None, ge=0, description="Exact chunk index.")
    model_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="Exact embedding model name.",
    )
    include_total: Optional[bool] = Field(
        default=None,
        description=(
            "Exact authorization-filtered total. It is included on the first "
            "page by default, may be requested on every page with true, and "
            "suppressed with false."
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


class ResourceGraphQuery(StrictQueryModel):
    depth: int = Field(
        default=1,
        ge=0,
        le=3,
        description="Maximum selected-edge distance from the root resource.",
    )
    direction: GraphDirection = Field(
        default=GraphDirection.BOTH,
        description="Lineage traversal direction. Associations remain undirected.",
    )
    relationship_kind: list[GraphRelationshipKind] = Field(
        default_factory=lambda: [
            GraphRelationshipKind.LINEAGE,
            GraphRelationshipKind.ASSOCIATION,
        ],
        min_length=1,
        description="Repeatable relationship kinds used for traversal and returned edges.",
    )
    relationship_type: Optional[list[GraphRelationshipTypeFilter]] = Field(
        default=None,
        min_length=1,
        description=(
            "Repeatable lineage relationship types. Use unspecified for stored null values. "
            "Associations are unaffected."
        ),
    )

    @model_validator(mode="after")
    def validate_relationship_filters(self):
        if (
            self.relationship_type is not None
            and GraphRelationshipKind.LINEAGE not in self.relationship_kind
        ):
            raise ValueError("relationship_type requires relationship_kind=lineage")
        return self


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
    affiliation: Optional[list[ResourceAffiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=RESOURCE_AFFILIATION_DESCRIPTION,
    )
    mfid: Optional[list[str]] = Field(default=None, min_length=1, max_length=MFID_FILTER_MAX, description=MFID_FILTER_DESCRIPTION)
    unique_id: Annotated[Optional[list[str]], Field(
        min_length=1,
        max_length=MFID_FILTER_MAX,
        deprecated=True,
        description="Deprecated alias of mfid. Cannot be combined with mfid.",
    )] = None
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
    creation_time_gte: Optional[UtcDatetime] = Field(default=None, description="Inclusive creation-time lower bound.")
    creation_time_lte: Optional[UtcDatetime] = Field(default=None, description="Inclusive creation-time upper bound.")
    modification_time_gte: Optional[UtcDatetime] = Field(default=None, description="Inclusive modification-time lower bound.")
    modification_time_lte: Optional[UtcDatetime] = Field(default=None, description="Inclusive modification-time upper bound.")
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
        unique_id = self.__dict__.get("unique_id")
        if self.mfid is not None and unique_id is not None:
            raise ValueError("mfid and unique_id cannot be combined; use mfid")
        if unique_id is not None:
            self.mfid = unique_id
            self.__dict__["unique_id"] = None
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
        description="Filter datasets assigned to this canonical instrument MFID. May be combined with a matching instrument_id.",
    )
    instrument_id: Optional[str] = Field(
        default=None,
        description="Filter datasets assigned to this instrument slug (case-insensitive). May be combined with a matching instrument_mfid.",
    )
    keyword: Optional[str] = None
    public: Optional[bool] = None
    dataset_name: Optional[str] = None
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact owner: unique_id, linked ORCID, username or email.",
    )
    owner_orcid: Annotated[Optional[str], Field(
        deprecated=True,
        description="Deprecated exact owner filter. Use owner_id, or affiliation=owner for the authenticated caller.",
    )] = None
    instrument_name: Annotated[Optional[str], Field(
        deprecated=True,
        description="Deprecated display-name filter. Matches the registered instrument's current name. Use instrument_mfid.",
    )] = None
    timestamp: Optional[str] = None
    size: Optional[int] = None
    data_format: Optional[str] = None
    data_type: Optional[str] = None
    measurement: Optional[str] = None
    session_name: Optional[str] = None
    session_name_is_null: Optional[bool] = Field(default=None, description="Filter by whether session_name is null.")
    measurement_is_null: Optional[bool] = Field(default=None, description="Filter by whether measurement is null.")
    data_format_is_null: Optional[bool] = Field(default=None, description="Filter by whether data_format is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Filter by whether the record has an owner.")
    instrument_mfid_is_null: Optional[bool] = Field(default=None, description="Filter by whether a canonical registered instrument is assigned.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Filter by whether a canonical primary project is assigned.")

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner_orcid = self.__dict__.get("owner_orcid")
        if self.owner_id is not None and owner_orcid is not None and self.owner_id != owner_orcid:
            raise ValueError("owner_id and owner_orcid must identify the same owner")
        if self.session_name_is_null is True and self.session_name is not None:
            raise ValueError("session_name_is_null=true cannot be combined with session_name")
        if self.measurement_is_null is True and self.measurement is not None:
            raise ValueError("measurement_is_null=true cannot be combined with measurement")
        if self.data_format_is_null is True and self.data_format is not None:
            raise ValueError("data_format_is_null=true cannot be combined with data_format")
        if self.owner_id_is_null is True and (self.owner_id is not None or owner_orcid is not None):
            raise ValueError("owner_id_is_null=true cannot be combined with an exact owner filter")
        if self.instrument_mfid_is_null is True and (self.instrument_mfid is not None or self.instrument_id is not None):
            raise ValueError("instrument_mfid_is_null=true cannot be combined with an instrument filter")
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
    public: Optional[bool] = None
    sample_name: Optional[str] = None
    sample_type: Optional[str] = None
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact owner: unique_id, linked ORCID, username or email.",
    )
    owner_orcid: Annotated[Optional[str], Field(
        deprecated=True,
        description="Deprecated exact owner filter. Use owner_id, or affiliation=owner for the authenticated caller.",
    )] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None
    sample_type_is_null: Optional[bool] = Field(default=None, description="Filter by whether sample_type is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Filter by whether the record has an owner.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Filter by whether a canonical primary project is assigned.")

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner_orcid = self.__dict__.get("owner_orcid")
        if self.owner_id is not None and owner_orcid is not None and self.owner_id != owner_orcid:
            raise ValueError("owner_id and owner_orcid must identify the same owner")
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
    affiliation: Optional[list[ResourceAffiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=RESOURCE_AFFILIATION_DESCRIPTION,
    )
    visibility: Literal["all", "public", "private"] = Field(default="all", description="Restrict the resources contributing to bucket counts by public visibility.")
    project_id: Optional[str] = Field(default=None, description="Project slug. May be combined with a matching project_mfid.")
    project_mfid: Optional[str] = Field(default=None, description="Canonical project MFID. May be combined with a matching project_id.")
    project_scope: Optional[ProjectScope] = Field(default=None, description="Project relationship to include. Requires a project filter and defaults to assigned.")
    creation_time_gte: Optional[UtcDatetime] = Field(default=None, description="Inclusive creation-time lower bound.")
    creation_time_lte: Optional[UtcDatetime] = Field(default=None, description="Inclusive creation-time upper bound.")
    modification_time_gte: Optional[UtcDatetime] = Field(default=None, description="Inclusive modification-time lower bound.")
    modification_time_lte: Optional[UtcDatetime] = Field(default=None, description="Inclusive modification-time upper bound.")
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
    instrument_mfid: Optional[str] = Field(default=None, description="Instrument MFID. May be combined with a matching instrument_id.")
    instrument_id: Optional[str] = Field(default=None, description="Instrument slug (case-insensitive). May be combined with a matching instrument_mfid.")
    session_name: Optional[str] = None
    measurement: Optional[str] = None
    data_format: Optional[str] = None
    owner_id: Optional[str] = Field(default=None, min_length=1, description="Exact owner: unique_id, linked ORCID, username or email.")
    session_name_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether session_name is null.")
    measurement_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether measurement is null.")
    data_format_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether data_format is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by whether they have an owner.")
    instrument_mfid_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by canonical instrument assignment.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Restrict contributing datasets by canonical primary project assignment.")

    @model_validator(mode="after")
    def validate_facet_filters(self):
        if self.session_name_is_null is True and self.session_name is not None:
            raise ValueError("session_name_is_null=true cannot be combined with session_name")
        if self.measurement_is_null is True and self.measurement is not None:
            raise ValueError("measurement_is_null=true cannot be combined with measurement")
        if self.data_format_is_null is True and self.data_format is not None:
            raise ValueError("data_format_is_null=true cannot be combined with data_format")
        if self.owner_id_is_null is True and self.owner_id is not None:
            raise ValueError("owner_id_is_null=true cannot be combined with owner_id")
        if self.instrument_mfid_is_null is True and (self.instrument_mfid is not None or self.instrument_id is not None):
            raise ValueError("instrument_mfid_is_null=true cannot be combined with an instrument filter")
        if self.project_mfid_is_null is True and (self.project_id is not None or self.project_mfid is not None):
            raise ValueError("project_mfid_is_null=true cannot be combined with a project filter")
        return self


class SampleFacetQuery(ResourceFacetQuery):
    field: SampleFacetField = Field(description="Sample field to group into facet buckets.")
    dataset_mfid: Optional[str] = None
    sample_type: Optional[str] = None
    owner_id: Optional[str] = Field(default=None, min_length=1, description="Exact owner: unique_id, linked ORCID, username or email.")
    sample_type_is_null: Optional[bool] = Field(default=None, description="Restrict contributing samples by whether sample_type is null.")
    owner_id_is_null: Optional[bool] = Field(default=None, description="Restrict contributing samples by whether they have an owner.")
    project_mfid_is_null: Optional[bool] = Field(default=None, description="Restrict contributing samples by canonical primary project assignment.")

    @model_validator(mode="after")
    def validate_facet_filters(self):
        if self.sample_type_is_null is True and self.sample_type is not None:
            raise ValueError("sample_type_is_null=true cannot be combined with sample_type")
        if self.owner_id_is_null is True and self.owner_id is not None:
            raise ValueError("owner_id_is_null=true cannot be combined with owner_id")
        if self.project_mfid_is_null is True and (self.project_id is not None or self.project_mfid is not None):
            raise ValueError("project_mfid_is_null=true cannot be combined with a project filter")
        return self


class ProjectListQuery(StrictQueryModel):
    project_id: Optional[str] = Field(default=None, description="Exact project slug.")
    mfid: Optional[list[str]] = Field(default=None, min_length=1, max_length=MFID_FILTER_MAX, description=MFID_FILTER_DESCRIPTION)
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
    member_ref: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=10,
        description=MEMBER_REF_DESCRIPTION,
    )
    member_role: Optional[list[AccessPermission]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description="Restrict member_ref to these project roles. Repeated values use OR semantics. Requires member_ref.",
    )
    active_since: Optional[UtcDatetime] = Field(default=None, description=ACTIVE_SINCE_DESCRIPTION)
    sort: Optional[ProjectSort] = Field(default=None, description=PROJECT_SORT_DESCRIPTION)
    direction: Optional[SortDirection] = Field(default=None, description=SORT_DIRECTION_DESCRIPTION)
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

    @field_validator("member_ref")
    @classmethod
    def validate_member_ref(cls, values):
        if values is None:
            return None
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("member_ref cannot contain blank values")
        return normalized

    @model_validator(mode="after")
    def validate_combinations(self):
        if self.include_members and self.project_id is None:
            raise ValueError("include_members requires an exact project_id filter")
        if self.member_role and not self.member_ref:
            raise ValueError("member_role requires member_ref")
        return self

    def normalized_result_filters(self) -> dict:
        return self.model_dump(
            mode="json",
            exclude={
                "cursor", "offset", "limit", "include_metadata",
                "include_members", "include_total", "sort", "direction",
            },
        )


class UserProjectListQuery(StrictQueryModel):
    role: Optional[list[AccessPermission]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description="Restrict to projects where the user holds one of these roles. Repeated values use OR semantics.",
    )
    include_metadata: bool = False
    include_total: Optional[bool] = Field(
        default=None,
        description="Exact total. Computed unless include_total=false.",
    )
    sort: Optional[ProjectSort] = Field(default=None, description=PROJECT_SORT_DESCRIPTION)
    direction: Optional[SortDirection] = Field(default=None, description=SORT_DIRECTION_DESCRIPTION)
    limit: int = Field(default=100, ge=1, le=1000)
    cursor: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=4096,
        description="Opaque continuation cursor returned by a previous page.",
    )
    offset: int = Field(default=0, ge=0)

    def normalized_result_filters(self) -> dict:
        return self.model_dump(
            mode="json",
            include={"role"},
        )


class InstrumentListQuery(StrictQueryModel):
    instrument_id: Optional[str] = Field(default=None, description="Exact instrument slug.")
    mfid: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=MFID_FILTER_MAX,
        description=MFID_FILTER_DESCRIPTION + " Searches every status unless status is also provided.",
    )
    affiliation: Optional[list[InstrumentAffiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=INSTRUMENT_AFFILIATION_DESCRIPTION,
    )
    include_metadata: bool = False
    include_owner: bool = False
    include_members: bool = Field(
        default=False,
        description=INSTRUMENT_MEMBERS_DESCRIPTION + " Requires an exact instrument_id filter.",
    )
    include_total: Optional[bool] = Field(
        default=None,
        description=(
            "Exact authorization-filtered total. Omit to preserve legacy endpoint behavior; "
            "true always computes it and false always omits it."
        ),
    )
    sort: Optional[InstrumentSort] = Field(default=None, description=INSTRUMENT_SORT_DESCRIPTION)
    direction: Optional[SortDirection] = Field(default=None, description=SORT_DIRECTION_DESCRIPTION)
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
        description="Exact owner: unique_id, linked ORCID, username or email.",
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
    active_since: Optional[UtcDatetime] = Field(default=None, description=ACTIVE_SINCE_DESCRIPTION)

    @model_validator(mode="after")
    def validate_affiliation(self):
        owner = self.__dict__.get("owner")
        if self.owner_id is not None and owner is not None and self.owner_id != owner:
            raise ValueError("owner_id and owner must identify the same owner")
        if self.include_members and self.instrument_id is None:
            raise ValueError("include_members requires an exact instrument_id filter")
        return self

    def normalized_result_filters(self) -> dict:
        values = self.model_dump(
            mode="json",
            exclude={
                "cursor", "offset", "limit", "include_metadata",
                "include_owner", "include_members", "include_total", "owner",
                "sort", "direction",
            },
        )
        values["owner_id"] = self.owner_id or self.__dict__.get("owner")
        if self.instrument_id is None and self.mfid is None and self.status is None:
            values["status"] = InstrumentStatus.ACTIVE.value
        return values


class UserListQuery(BaseModel):
    unique_id: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=MFID_FILTER_MAX,
        description=f"Repeatable exact ORCID or service-account MFID, at most {MFID_FILTER_MAX} values, OR semantics.",
    )
    username: Optional[list[str]] = Field(
        default=None,
        min_length=1,
        max_length=MFID_FILTER_MAX,
        description=f"Repeatable exact username, matched case-insensitively, at most {MFID_FILTER_MAX} values, OR semantics.",
    )
    email: Optional[str] = Field(
        default=None,
        description="Exact email address, matched case-insensitively.",
    )
    permissive: bool = Field(
        default=True,
        description=(
            "Matching mode for first_name and last_name. True is a "
            "case-insensitive substring match, False an exact match."
        ),
    )
    is_service_account: Optional[bool] = Field(
        default=None,
        description="Restrict results to service accounts or human users.",
    )
    pending: bool = Field(
        default=False,
        description="List only accounts waiting for approval instead of active ones. Platform administrators only.",
    )
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    q: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Case-insensitive substring search. Searches the fields named in search_fields, "
            "or by default first_name, last_name, username, unique_id, and, for platform "
            "administrators, email."
        ),
    )
    search_fields: Optional[list[UserSearchField]] = Field(
        default=None,
        min_length=1,
        description=(
            "Repeatable list of fields searched by q, with OR semantics. Requires q. "
            "email is administrator only; other callers receive 403."
        ),
    )
    active_since: Optional[UtcDatetime] = Field(
        default=None,
        description="Users active at or after this time (hourly precision). Administrator only.",
    )
    inactive_since: Optional[UtcDatetime] = Field(
        default=None,
        description="Users not active since this time, including users never seen active. Administrator only.",
    )
    never_active: Optional[bool] = Field(
        default=None,
        description="True for users never seen active, false for users seen at least once. Administrator only.",
    )
    platform_role: Optional[list[PlatformRole]] = Field(
        default=None,
        min_length=1,
        description=(
            "Repeatable platform-role filter, administrator only. "
            "Repeated values use OR semantics. Other callers receive 403."
        ),
    )
    include_total: Optional[bool] = Field(
        default=None,
        description=(
            "Exact authorization-filtered total. Omission preserves the "
            "legacy behavior that always returns a total."
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
    sort: Optional[UserSort] = Field(
        default=None,
        description=(
            "Explicit ordering, case-insensitive, with unique_id as tie-breaker and nulls last. "
            "name sorts by last name then first name. email and last_active are administrator only. "
            "Omission keeps the default last name, first name order."
        ),
    )
    direction: SortDirection = Field(
        default=SortDirection.ASC,
        description="Direction for an explicit sort.",
    )

    @model_validator(mode="after")
    def validate_search_fields(self):
        if self.search_fields and self.q is None:
            raise ValueError("search_fields requires q")
        return self

    def cursor_filters(self) -> dict:
        return self.model_dump(
            mode="json",
            exclude={"cursor", "offset", "limit", "sort", "direction", "include_total"},
        )


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
    affiliation: Optional[list[ResourceAffiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=RESOURCE_AFFILIATION_DESCRIPTION,
    )
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact owner: unique_id, linked ORCID, username or email.",
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
        return self


class InstrumentSearchQuery(SearchQuery):
    owner_id: Optional[str] = Field(
        default=None,
        min_length=1,
        description="Exact owner: unique_id, linked ORCID, username or email.",
    )
    status: Optional[InstrumentStatus] = Field(
        default=None,
        description="Restrict results to one lifecycle status.",
    )
    affiliation: Optional[list[InstrumentAffiliation]] = Field(
        default=None,
        min_length=1,
        max_length=5,
        description=INSTRUMENT_AFFILIATION_DESCRIPTION,
    )
    include_owner: bool = False

