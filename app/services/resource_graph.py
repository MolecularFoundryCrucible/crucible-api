from dataclasses import dataclass
from typing import Optional

from sqlalchemy import String, case, cast, func, literal, or_, union_all
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.tables import Dataset, DatasetLink, DatasetSampleLink, Sample, SampleLink
from app.models.links import LinkRelationshipType
from app.models.query_params import ResourceGraphQuery
from app.models.resource_graph import (
    AssociationGraphEdge,
    DatasetGraphNode,
    GraphDirection,
    GraphExpansionAvailability,
    GraphRelationshipKind,
    GraphRelationshipTypeFilter,
    GraphTruncationReason,
    LineageGraphEdge,
    ResourceGraphResponse,
    SampleGraphNode,
)
from app.utils.auth_utils import permission_filter
from app.utils.instrument_utils import resolve_instrument_mfids_to_references
from app.utils.permissions import Permission, Principal
from app.utils.project_utils import resolve_project_mfids_to_references
from app.utils.sql_utils import deletion_filter_for


GRAPH_NODE_LIMIT = 500
GRAPH_EDGE_LIMIT = 1000
UPSTREAM_LINEAGE = "upstream_lineage"
DOWNSTREAM_LINEAGE = "downstream_lineage"
ASSOCIATIONS = "associations"

NodeKey = tuple[str, int]


@dataclass(frozen=True)
class TraversalEdge:
    traversal_source: NodeKey
    traversal_source_mfid: str
    neighbor: NodeKey
    neighbor_mfid: str
    kind: str
    edge_source_type: str
    edge_source_mfid: str
    edge_target_mfid: str
    relationship_type: Optional[str]

    @property
    def edge_id(self) -> str:
        if self.kind == GraphRelationshipKind.LINEAGE.value:
            return (
                f"lineage:{self.edge_source_type}:"
                f"{self.edge_source_mfid}:{self.edge_target_mfid}"
            )
        return (
            f"association:sample:{self.edge_source_mfid}:"
            f"dataset:{self.edge_target_mfid}"
        )

    def to_response(self):
        if self.kind == GraphRelationshipKind.LINEAGE.value:
            relationship_type = (
                LinkRelationshipType(self.relationship_type)
                if self.relationship_type is not None
                else None
            )
            return LineageGraphEdge(
                id=self.edge_id,
                source_mfid=self.edge_source_mfid,
                target_mfid=self.edge_target_mfid,
                kind="lineage",
                directed=True,
                relationship_type=relationship_type,
            )
        return AssociationGraphEdge(
            id=self.edge_id,
            source_mfid=self.edge_source_mfid,
            target_mfid=self.edge_target_mfid,
            kind="association",
            directed=False,
            relationship_type=None,
        )


def _lineage_type_condition(column, filters):
    if filters is None:
        return None
    clauses = []
    values = [
        value.value
        for value in filters
        if value is not GraphRelationshipTypeFilter.UNSPECIFIED
    ]
    if values:
        clauses.append(cast(column, String).in_(values))
    if GraphRelationshipTypeFilter.UNSPECIFIED in filters:
        clauses.append(column.is_(None))
    return or_(*clauses)


def _candidate_selects(
    principal: Principal,
    frontier: set[NodeKey],
    params: ResourceGraphQuery,
    excluded: Optional[set[NodeKey]] = None,
):
    sample_ids = [node_id for kind, node_id in frontier if kind == "sample"]
    dataset_ids = [node_id for kind, node_id in frontier if kind == "dataset"]
    excluded_sample_ids = [
        node_id for kind, node_id in excluded or set() if kind == "sample"
    ]
    excluded_dataset_ids = [
        node_id for kind, node_id in excluded or set() if kind == "dataset"
    ]
    kinds = set(params.relationship_kind)
    branches = []

    def target_filters(model, resource_type: str):
        filters = [
            permission_filter(principal, model, Permission.READ),
            deletion_filter_for(model),
        ]
        excluded_ids = (
            excluded_sample_ids if resource_type == "sample" else excluded_dataset_ids
        )
        if excluded_ids:
            filters.append(model.id.not_in(excluded_ids))
        return filters

    lineage_filter = _lineage_type_condition(
        SampleLink.relationship_type,
        params.relationship_type,
    )

    if GraphRelationshipKind.LINEAGE in kinds and sample_ids:
        parent = aliased(Sample)
        child = aliased(Sample)
        common = [lineage_filter] if lineage_filter is not None else []
        if params.direction in (GraphDirection.BOTH, GraphDirection.DOWNSTREAM):
            branches.append(
                select(
                    literal("sample").label("traversal_source_type"),
                    parent.id.label("traversal_source_id"),
                    parent.unique_id.label("traversal_source_mfid"),
                    literal("sample").label("neighbor_type"),
                    child.id.label("neighbor_id"),
                    child.unique_id.label("neighbor_mfid"),
                    literal("lineage").label("kind"),
                    literal("sample").label("edge_source_type"),
                    parent.unique_id.label("edge_source_mfid"),
                    child.unique_id.label("edge_target_mfid"),
                    cast(SampleLink.relationship_type, String).label("relationship_type"),
                )
                .select_from(SampleLink)
                .join(parent, parent.id == SampleLink.parent_sample_id)
                .join(child, child.id == SampleLink.sample_id)
                .where(
                    parent.id.in_(sample_ids),
                    parent.id != child.id,
                    *target_filters(child, "sample"),
                    *common,
                )
            )
        if params.direction in (GraphDirection.BOTH, GraphDirection.UPSTREAM):
            branches.append(
                select(
                    literal("sample").label("traversal_source_type"),
                    child.id.label("traversal_source_id"),
                    child.unique_id.label("traversal_source_mfid"),
                    literal("sample").label("neighbor_type"),
                    parent.id.label("neighbor_id"),
                    parent.unique_id.label("neighbor_mfid"),
                    literal("lineage").label("kind"),
                    literal("sample").label("edge_source_type"),
                    parent.unique_id.label("edge_source_mfid"),
                    child.unique_id.label("edge_target_mfid"),
                    cast(SampleLink.relationship_type, String).label("relationship_type"),
                )
                .select_from(SampleLink)
                .join(parent, parent.id == SampleLink.parent_sample_id)
                .join(child, child.id == SampleLink.sample_id)
                .where(
                    child.id.in_(sample_ids),
                    parent.id != child.id,
                    *target_filters(parent, "sample"),
                    *common,
                )
            )

    lineage_filter = _lineage_type_condition(
        DatasetLink.relationship_type,
        params.relationship_type,
    )

    if GraphRelationshipKind.LINEAGE in kinds and dataset_ids:
        parent = aliased(Dataset)
        child = aliased(Dataset)
        common = [lineage_filter] if lineage_filter is not None else []
        if params.direction in (GraphDirection.BOTH, GraphDirection.DOWNSTREAM):
            branches.append(
                select(
                    literal("dataset").label("traversal_source_type"),
                    parent.id.label("traversal_source_id"),
                    parent.unique_id.label("traversal_source_mfid"),
                    literal("dataset").label("neighbor_type"),
                    child.id.label("neighbor_id"),
                    child.unique_id.label("neighbor_mfid"),
                    literal("lineage").label("kind"),
                    literal("dataset").label("edge_source_type"),
                    parent.unique_id.label("edge_source_mfid"),
                    child.unique_id.label("edge_target_mfid"),
                    cast(DatasetLink.relationship_type, String).label("relationship_type"),
                )
                .select_from(DatasetLink)
                .join(parent, parent.id == DatasetLink.parent_dataset_id)
                .join(child, child.id == DatasetLink.child_dataset_id)
                .where(
                    parent.id.in_(dataset_ids),
                    parent.id != child.id,
                    *target_filters(child, "dataset"),
                    *common,
                )
            )
        if params.direction in (GraphDirection.BOTH, GraphDirection.UPSTREAM):
            branches.append(
                select(
                    literal("dataset").label("traversal_source_type"),
                    child.id.label("traversal_source_id"),
                    child.unique_id.label("traversal_source_mfid"),
                    literal("dataset").label("neighbor_type"),
                    parent.id.label("neighbor_id"),
                    parent.unique_id.label("neighbor_mfid"),
                    literal("lineage").label("kind"),
                    literal("dataset").label("edge_source_type"),
                    parent.unique_id.label("edge_source_mfid"),
                    child.unique_id.label("edge_target_mfid"),
                    cast(DatasetLink.relationship_type, String).label("relationship_type"),
                )
                .select_from(DatasetLink)
                .join(parent, parent.id == DatasetLink.parent_dataset_id)
                .join(child, child.id == DatasetLink.child_dataset_id)
                .where(
                    child.id.in_(dataset_ids),
                    parent.id != child.id,
                    *target_filters(parent, "dataset"),
                    *common,
                )
            )

    if GraphRelationshipKind.ASSOCIATION in kinds and sample_ids:
        sample = aliased(Sample)
        dataset = aliased(Dataset)
        branches.append(
            select(
                literal("sample").label("traversal_source_type"),
                sample.id.label("traversal_source_id"),
                sample.unique_id.label("traversal_source_mfid"),
                literal("dataset").label("neighbor_type"),
                dataset.id.label("neighbor_id"),
                dataset.unique_id.label("neighbor_mfid"),
                literal("association").label("kind"),
                literal("sample").label("edge_source_type"),
                sample.unique_id.label("edge_source_mfid"),
                dataset.unique_id.label("edge_target_mfid"),
                literal(None, type_=String).label("relationship_type"),
            )
            .select_from(DatasetSampleLink)
            .join(sample, sample.id == DatasetSampleLink.sample_id)
            .join(dataset, dataset.id == DatasetSampleLink.dataset_id)
            .where(
                sample.id.in_(sample_ids),
                *target_filters(dataset, "dataset"),
            )
        )

    if GraphRelationshipKind.ASSOCIATION in kinds and dataset_ids:
        sample = aliased(Sample)
        dataset = aliased(Dataset)
        branches.append(
            select(
                literal("dataset").label("traversal_source_type"),
                dataset.id.label("traversal_source_id"),
                dataset.unique_id.label("traversal_source_mfid"),
                literal("sample").label("neighbor_type"),
                sample.id.label("neighbor_id"),
                sample.unique_id.label("neighbor_mfid"),
                literal("association").label("kind"),
                literal("sample").label("edge_source_type"),
                sample.unique_id.label("edge_source_mfid"),
                dataset.unique_id.label("edge_target_mfid"),
                literal(None, type_=String).label("relationship_type"),
            )
            .select_from(DatasetSampleLink)
            .join(sample, sample.id == DatasetSampleLink.sample_id)
            .join(dataset, dataset.id == DatasetSampleLink.dataset_id)
            .where(
                dataset.id.in_(dataset_ids),
                *target_filters(sample, "sample"),
            )
        )

    return branches


def _candidate_edges(
    session: Session,
    principal: Principal,
    frontier: set[NodeKey],
    params: ResourceGraphQuery,
    excluded: Optional[set[NodeKey]] = None,
) -> tuple[list[TraversalEdge], bool]:
    branches = _candidate_selects(principal, frontier, params, excluded)
    if not branches:
        return [], False

    candidates = union_all(*branches).subquery()
    statement = (
        select(candidates)
        .order_by(
            candidates.c.neighbor_type,
            candidates.c.neighbor_mfid,
            candidates.c.traversal_source_type,
            candidates.c.traversal_source_mfid,
            candidates.c.kind,
            candidates.c.edge_source_mfid,
            candidates.c.edge_target_mfid,
            func.coalesce(candidates.c.relationship_type, ""),
        )
        .limit(GRAPH_EDGE_LIMIT + 1)
    )
    rows = session.execute(statement).all()
    overflow = len(rows) > GRAPH_EDGE_LIMIT
    edges = [
        TraversalEdge(
            traversal_source=(row[0], row[1]),
            traversal_source_mfid=row[2],
            neighbor=(row[3], row[4]),
            neighbor_mfid=row[5],
            kind=row[6],
            edge_source_type=row[7],
            edge_source_mfid=row[8],
            edge_target_mfid=row[9],
            relationship_type=row[10],
        )
        for row in rows[:GRAPH_EDGE_LIMIT]
    ]
    return edges, overflow


def _relationship_counts(
    session: Session,
    principal: Principal,
    nodes: set[NodeKey],
    params: ResourceGraphQuery,
) -> dict[tuple[str, str], int]:
    availability_params = ResourceGraphQuery(
        depth=0,
        direction=GraphDirection.BOTH,
        relationship_kind=[
            GraphRelationshipKind.LINEAGE,
            GraphRelationshipKind.ASSOCIATION,
        ],
        relationship_type=params.relationship_type,
    )
    branches = _candidate_selects(principal, nodes, availability_params)
    if not branches:
        return {}

    candidates = union_all(*branches).subquery()
    channel = case(
        (candidates.c.kind == GraphRelationshipKind.ASSOCIATION.value, ASSOCIATIONS),
        (
            candidates.c.traversal_source_mfid == candidates.c.edge_source_mfid,
            DOWNSTREAM_LINEAGE,
        ),
        else_=UPSTREAM_LINEAGE,
    ).label("channel")
    rows = session.execute(
        select(
            candidates.c.traversal_source_mfid,
            channel,
            func.count().label("relationship_count"),
        ).group_by(candidates.c.traversal_source_mfid, channel)
    ).all()
    return {(row[0], row[1]): row[2] for row in rows}


def _returned_relationship_counts(edges) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}

    def increment(mfid: str, channel: str):
        key = (mfid, channel)
        counts[key] = counts.get(key, 0) + 1

    for edge in edges:
        if edge.kind == GraphRelationshipKind.LINEAGE.value:
            increment(edge.source_mfid, DOWNSTREAM_LINEAGE)
            increment(edge.target_mfid, UPSTREAM_LINEAGE)
        else:
            increment(edge.source_mfid, ASSOCIATIONS)
            increment(edge.target_mfid, ASSOCIATIONS)
    return counts


def _expansion_availability(
    session: Session,
    principal: Principal,
    nodes: set[NodeKey],
    params: ResourceGraphQuery,
    edges,
) -> dict[str, GraphExpansionAvailability]:
    totals = _relationship_counts(session, principal, nodes, params)
    returned = _returned_relationship_counts(edges)
    mfids = {mfid for mfid, _ in totals} | {mfid for mfid, _ in returned}
    return {
        mfid: GraphExpansionAvailability(
            upstream_lineage=(
                totals.get((mfid, UPSTREAM_LINEAGE), 0)
                > returned.get((mfid, UPSTREAM_LINEAGE), 0)
            ),
            downstream_lineage=(
                totals.get((mfid, DOWNSTREAM_LINEAGE), 0)
                > returned.get((mfid, DOWNSTREAM_LINEAGE), 0)
            ),
            associations=(
                totals.get((mfid, ASSOCIATIONS), 0)
                > returned.get((mfid, ASSOCIATIONS), 0)
            ),
        )
        for mfid in mfids
    }


def _aggregate_availability(
    availability: GraphExpansionAvailability,
    params: ResourceGraphQuery,
) -> Optional[bool]:
    values = []
    kinds = set(params.relationship_kind)
    if GraphRelationshipKind.LINEAGE in kinds:
        if params.direction in (GraphDirection.BOTH, GraphDirection.UPSTREAM):
            values.append(availability.upstream_lineage)
        if params.direction in (GraphDirection.BOTH, GraphDirection.DOWNSTREAM):
            values.append(availability.downstream_lineage)
    if GraphRelationshipKind.ASSOCIATION in kinds:
        values.append(availability.associations)
    if any(value is True for value in values):
        return True
    if any(value is None for value in values):
        return None
    return False


def _find_root(session: Session, principal: Principal, root_mfid: str):
    sample = session.exec(
        select(Sample)
        .where(Sample.unique_id == root_mfid)
        .where(permission_filter(principal, Sample, Permission.READ))
        .where(deletion_filter_for(Sample))
    ).one_or_none()
    if sample is not None:
        return "sample", sample.id

    dataset = session.exec(
        select(Dataset)
        .where(Dataset.unique_id == root_mfid)
        .where(permission_filter(principal, Dataset, Permission.READ))
        .where(deletion_filter_for(Dataset))
    ).one_or_none()
    if dataset is not None:
        return "dataset", dataset.id
    return None


def _induced_edge_selects(visited: set[NodeKey], params: ResourceGraphQuery):
    sample_ids = [node_id for kind, node_id in visited if kind == "sample"]
    dataset_ids = [node_id for kind, node_id in visited if kind == "dataset"]
    kinds = set(params.relationship_kind)
    branches = []

    if GraphRelationshipKind.LINEAGE in kinds and sample_ids:
        parent = aliased(Sample)
        child = aliased(Sample)
        statement = (
            select(
                literal("lineage").label("kind"),
                literal("sample").label("edge_source_type"),
                parent.unique_id.label("edge_source_mfid"),
                child.unique_id.label("edge_target_mfid"),
                cast(SampleLink.relationship_type, String).label("relationship_type"),
            )
            .select_from(SampleLink)
            .join(parent, parent.id == SampleLink.parent_sample_id)
            .join(child, child.id == SampleLink.sample_id)
            .where(
                parent.id.in_(sample_ids),
                child.id.in_(sample_ids),
                parent.id != child.id,
            )
        )
        relationship_filter = _lineage_type_condition(
            SampleLink.relationship_type,
            params.relationship_type,
        )
        if relationship_filter is not None:
            statement = statement.where(relationship_filter)
        branches.append(statement)

    if GraphRelationshipKind.LINEAGE in kinds and dataset_ids:
        parent = aliased(Dataset)
        child = aliased(Dataset)
        statement = (
            select(
                literal("lineage").label("kind"),
                literal("dataset").label("edge_source_type"),
                parent.unique_id.label("edge_source_mfid"),
                child.unique_id.label("edge_target_mfid"),
                cast(DatasetLink.relationship_type, String).label("relationship_type"),
            )
            .select_from(DatasetLink)
            .join(parent, parent.id == DatasetLink.parent_dataset_id)
            .join(child, child.id == DatasetLink.child_dataset_id)
            .where(
                parent.id.in_(dataset_ids),
                child.id.in_(dataset_ids),
                parent.id != child.id,
            )
        )
        relationship_filter = _lineage_type_condition(
            DatasetLink.relationship_type,
            params.relationship_type,
        )
        if relationship_filter is not None:
            statement = statement.where(relationship_filter)
        branches.append(statement)

    if (
        GraphRelationshipKind.ASSOCIATION in kinds
        and sample_ids
        and dataset_ids
    ):
        sample = aliased(Sample)
        dataset = aliased(Dataset)
        branches.append(
            select(
                literal("association").label("kind"),
                literal("sample").label("edge_source_type"),
                sample.unique_id.label("edge_source_mfid"),
                dataset.unique_id.label("edge_target_mfid"),
                literal(None, type_=String).label("relationship_type"),
            )
            .select_from(DatasetSampleLink)
            .join(sample, sample.id == DatasetSampleLink.sample_id)
            .join(dataset, dataset.id == DatasetSampleLink.dataset_id)
            .where(
                sample.id.in_(sample_ids),
                dataset.id.in_(dataset_ids),
            )
        )

    return branches


def _induced_edges(
    session: Session,
    visited: set[NodeKey],
    params: ResourceGraphQuery,
    discovery_edges: dict[str, TraversalEdge],
) -> tuple[list, bool]:
    branches = _induced_edge_selects(visited, params)
    if not branches:
        return [], False

    edges = union_all(*branches).subquery()
    statement = (
        select(edges)
        .order_by(
            edges.c.kind,
            edges.c.edge_source_type,
            edges.c.edge_source_mfid,
            edges.c.edge_target_mfid,
            func.coalesce(edges.c.relationship_type, ""),
        )
        .limit(GRAPH_EDGE_LIMIT + 1)
    )
    rows = session.execute(statement).all()
    overflow = len(rows) > GRAPH_EDGE_LIMIT
    selected: dict[str, TraversalEdge] = dict(discovery_edges)
    for row in rows[:GRAPH_EDGE_LIMIT]:
        edge = TraversalEdge(
            traversal_source=(row[1], 0),
            traversal_source_mfid=row[2],
            neighbor=(row[1], 0),
            neighbor_mfid=row[3],
            kind=row[0],
            edge_source_type=row[1],
            edge_source_mfid=row[2],
            edge_target_mfid=row[3],
            relationship_type=row[4],
        )
        selected.setdefault(edge.edge_id, edge)

    discovery_ids = set(discovery_edges)
    ordered = sorted(
        selected.values(),
        key=lambda edge: (
            edge.edge_id not in discovery_ids,
            edge.kind,
            edge.edge_source_type,
            edge.edge_source_mfid,
            edge.edge_target_mfid,
            edge.relationship_type or "",
        ),
    )
    if len(ordered) > GRAPH_EDGE_LIMIT:
        overflow = True
        ordered = ordered[:GRAPH_EDGE_LIMIT]
    response_edges = [edge.to_response() for edge in ordered]
    response_edges.sort(
        key=lambda edge: (
            edge.kind,
            edge.source_mfid,
            edge.target_mfid,
            edge.relationship_type.value if edge.relationship_type is not None else "",
        )
    )
    return response_edges, overflow


def _graph_nodes(
    session: Session,
    depths: dict[NodeKey, int],
    availability_by_mfid: dict[str, GraphExpansionAvailability],
    params: ResourceGraphQuery,
):
    sample_ids = [node_id for kind, node_id in depths if kind == "sample"]
    dataset_ids = [node_id for kind, node_id in depths if kind == "dataset"]
    samples = (
        session.exec(select(Sample).where(Sample.id.in_(sample_ids))).all()
        if sample_ids
        else []
    )
    datasets = (
        session.exec(select(Dataset).where(Dataset.id.in_(dataset_ids))).all()
        if dataset_ids
        else []
    )
    project_references = resolve_project_mfids_to_references(
        [resource.project_mfid for resource in [*samples, *datasets]],
        session,
    )
    instrument_references = resolve_instrument_mfids_to_references(
        [dataset.instrument_mfid for dataset in datasets],
        session,
    )

    def availability_for(mfid: str):
        return availability_by_mfid.get(
            mfid,
            GraphExpansionAvailability(
                upstream_lineage=False,
                downstream_lineage=False,
                associations=False,
            ),
        )

    nodes = [
        SampleGraphNode(
            mfid=sample.unique_id,
            name=sample.sample_name,
            traversal_depth=depths[("sample", sample.id)],
            has_more_relationships=_aggregate_availability(
                availability_for(sample.unique_id),
                params,
            ),
            expansion_availability=availability_for(sample.unique_id),
            project=project_references.get(sample.project_mfid),
            resource_type="sample",
            sample_type=sample.sample_type,
        )
        for sample in samples
    ]
    nodes.extend(
        DatasetGraphNode(
            mfid=dataset.unique_id,
            name=dataset.dataset_name,
            traversal_depth=depths[("dataset", dataset.id)],
            has_more_relationships=_aggregate_availability(
                availability_for(dataset.unique_id),
                params,
            ),
            expansion_availability=availability_for(dataset.unique_id),
            project=project_references.get(dataset.project_mfid),
            measurement=dataset.measurement,
            data_format=dataset.data_format,
            instrument=instrument_references.get(dataset.instrument_mfid),
            resource_type="dataset",
        )
        for dataset in datasets
    )
    nodes.sort(key=lambda node: (node.traversal_depth, node.resource_type, node.mfid))
    return nodes


def build_resource_graph(
    session: Session,
    principal: Principal,
    root_mfid: str,
    params: ResourceGraphQuery,
) -> Optional[ResourceGraphResponse]:
    root = _find_root(session, principal, root_mfid)
    if root is None:
        return None

    depths: dict[NodeKey, int] = {root: 0}
    discovery_edges: dict[str, TraversalEdge] = {}
    frontier = {root}
    truncation_reasons: set[GraphTruncationReason] = set()

    for traversal_depth in range(1, params.depth + 1):
        candidates, edge_overflow = _candidate_edges(
            session,
            principal,
            frontier,
            params,
        )
        if edge_overflow:
            truncation_reasons.add(GraphTruncationReason.EDGE_LIMIT)

        candidates_by_neighbor: dict[NodeKey, list[TraversalEdge]] = {}
        for edge in candidates:
            candidates_by_neighbor.setdefault(edge.neighbor, []).append(edge)

        new_neighbors = [
            neighbor
            for neighbor in candidates_by_neighbor
            if neighbor not in depths
        ]
        new_neighbors.sort(
            key=lambda node: (
                node[0],
                candidates_by_neighbor[node][0].neighbor_mfid,
            )
        )
        remaining = GRAPH_NODE_LIMIT - len(depths)
        selected_neighbors = set(new_neighbors[:remaining])
        omitted_neighbors = set(new_neighbors[remaining:])
        if omitted_neighbors:
            truncation_reasons.add(GraphTruncationReason.NODE_LIMIT)

        for neighbor in selected_neighbors:
            depths[neighbor] = traversal_depth
            discovery = candidates_by_neighbor[neighbor][0]
            discovery_edges.setdefault(discovery.edge_id, discovery)

        frontier = selected_neighbors
        if edge_overflow or omitted_neighbors or not frontier:
            break

    edges, induced_edge_overflow = _induced_edges(
        session,
        set(depths),
        params,
        discovery_edges,
    )
    if induced_edge_overflow:
        truncation_reasons.add(GraphTruncationReason.EDGE_LIMIT)

    expansion_availability = _expansion_availability(
        session,
        principal,
        set(depths),
        params,
        edges,
    )
    nodes = _graph_nodes(session, depths, expansion_availability, params)
    ordered_reasons = sorted(truncation_reasons, key=lambda reason: reason.value)
    return ResourceGraphResponse(
        root_mfid=root_mfid,
        nodes=nodes,
        edges=edges,
        requested_depth=params.depth,
        returned_depth=max(depths.values()),
        has_more=(
            bool(truncation_reasons)
            or any(node.has_more_relationships is True for node in nodes)
        ),
        truncated=bool(truncation_reasons),
        truncation_reasons=ordered_reasons,
        node_limit=GRAPH_NODE_LIMIT,
        edge_limit=GRAPH_EDGE_LIMIT,
    )
