from enum import Enum
from typing import Annotated, Literal, Optional, Union

from pydantic import Field
from sqlmodel import SQLModel

from app.models.instrument import InstrumentReferenceRead
from app.models.links import LinkRelationshipType
from app.models.projects import ProjectReferenceRead


class GraphRelationshipKind(str, Enum):
    LINEAGE = "lineage"
    ASSOCIATION = "association"


class GraphDirection(str, Enum):
    BOTH = "both"
    UPSTREAM = "upstream"
    DOWNSTREAM = "downstream"


class GraphRelationshipTypeFilter(str, Enum):
    IS_DERIVED_FROM = "is_derived_from"
    IS_PART_OF = "is_part_of"
    UNSPECIFIED = "unspecified"


class GraphTruncationReason(str, Enum):
    NODE_LIMIT = "node_limit"
    EDGE_LIMIT = "edge_limit"


class GraphExpansionAvailability(SQLModel):
    upstream_lineage: bool
    downstream_lineage: bool
    associations: bool


class GraphNodeBase(SQLModel):
    mfid: str
    name: Optional[str]
    traversal_depth: int
    has_more_relationships: Optional[bool]
    expansion_availability: GraphExpansionAvailability
    project: Optional[ProjectReferenceRead]


class SampleGraphNode(GraphNodeBase):
    resource_type: Literal["sample"]
    sample_type: Optional[str]


class DatasetGraphNode(GraphNodeBase):
    resource_type: Literal["dataset"]
    measurement: Optional[str]
    data_format: Optional[str]
    instrument: Optional[InstrumentReferenceRead]


ResourceGraphNode = Annotated[
    Union[SampleGraphNode, DatasetGraphNode],
    Field(discriminator="resource_type"),
]


class GraphEdgeBase(SQLModel):
    id: str
    source_mfid: str
    target_mfid: str


class LineageGraphEdge(GraphEdgeBase):
    kind: Literal["lineage"]
    directed: Literal[True]
    relationship_type: Optional[LinkRelationshipType]


class AssociationGraphEdge(GraphEdgeBase):
    kind: Literal["association"]
    directed: Literal[False]
    relationship_type: None


ResourceGraphEdge = Annotated[
    Union[LineageGraphEdge, AssociationGraphEdge],
    Field(discriminator="kind"),
]


class ResourceGraphResponse(SQLModel):
    root_mfid: str
    nodes: list[ResourceGraphNode]
    edges: list[ResourceGraphEdge]
    requested_depth: int
    returned_depth: int
    has_more: bool
    truncated: bool
    truncation_reasons: list[GraphTruncationReason]
    node_limit: int
    edge_limit: int
