import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.api.main import app
from app.api.routes.resource_graph import read_resource_graph
from app.tables import (
    AccessGroup,
    Dataset,
    DatasetLink,
    DatasetSampleLink,
    Project,
    Resource,
    ResourceACL,
    Sample,
    SampleLink,
)
from app.models.query_params import ResourceGraphQuery
from app.models.resource_graph import (
    GraphDirection,
    GraphRelationshipKind,
    GraphRelationshipTypeFilter,
)
import app.services.resource_graph as resource_graph_service
from app.services.resource_graph import build_resource_graph
from app.utils.permissions import GroupMembership, Principal, Role


def _setup(db_engine):
    with Session(db_engine) as session:
        reader_group = AccessGroup(group_name="resource-graph-readers")
        private_group = AccessGroup(group_name="resource-graph-private")
        project = Project(
            unique_id="0tm5hcgs6knza000zgnh65r6v8",
            project_id="resource-graph-project",
            organization="LBNL",
            project_lead_orcid="graph-user",
            title="Resource graph project",
        )
        session.add_all([reader_group, private_group, project])
        session.commit()
        session.refresh(reader_group)
        session.refresh(private_group)

        sample_root = Sample(
            unique_id="sample-root",
            sample_name="Root sample",
            sample_type="powder",
            project_mfid=project.unique_id,
        )
        sample_child = Sample(
            unique_id="sample-child",
            sample_name="Child sample",
            project_mfid=project.unique_id,
        )
        sample_grandchild = Sample(
            unique_id="sample-grandchild",
            sample_name="Grandchild sample",
            project_mfid=project.unique_id,
        )
        sample_hidden = Sample(
            unique_id="sample-hidden",
            sample_name="Hidden sample",
            project_mfid=project.unique_id,
        )
        sample_beyond_hidden = Sample(
            unique_id="sample-beyond-hidden",
            sample_name="Visible beyond hidden",
            project_mfid=project.unique_id,
        )
        dataset_root = Dataset(
            unique_id="dataset-root",
            dataset_name="Root dataset",
            measurement="XAS",
            project_mfid=project.unique_id,
        )
        dataset_child = Dataset(
            unique_id="dataset-child",
            dataset_name="Child dataset",
            project_mfid=project.unique_id,
        )
        resources = [
            sample_root,
            sample_child,
            sample_grandchild,
            sample_hidden,
            sample_beyond_hidden,
            dataset_root,
            dataset_child,
        ]
        session.add_all(resources)
        session.commit()
        for resource in resources:
            session.refresh(resource)
            resource_type = "sample" if isinstance(resource, Sample) else "dataset"
            session.add(Resource(mfid=resource.unique_id, resource_type=resource_type))
            session.add(ResourceACL(
                mfid=resource.unique_id,
                accessgroup_id=(
                    private_group.id if resource is sample_hidden else reader_group.id
                ),
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ))

        session.add_all([
            SampleLink(
                parent_sample_id=sample_root.id,
                sample_id=sample_child.id,
                relationship_type="is_derived_from",
            ),
            SampleLink(
                parent_sample_id=sample_child.id,
                sample_id=sample_grandchild.id,
            ),
            SampleLink(
                parent_sample_id=sample_root.id,
                sample_id=sample_hidden.id,
                relationship_type="is_part_of",
            ),
            SampleLink(
                parent_sample_id=sample_hidden.id,
                sample_id=sample_beyond_hidden.id,
            ),
            DatasetLink(
                parent_dataset_id=dataset_root.id,
                child_dataset_id=dataset_child.id,
                relationship_type="is_part_of",
            ),
            DatasetSampleLink(
                sample_id=sample_child.id,
                dataset_id=dataset_root.id,
            ),
        ])
        session.commit()
        return reader_group.id


def _principal(group_id: int) -> Principal:
    return Principal(
        user_id="graph-user",
        groups=(GroupMembership(
            id=group_id,
            group_name="resource-graph-readers",
            role=Role.VIEWER,
        ),),
    )


def _graph(db_engine, root_mfid: str, group_id: int, **kwargs):
    with Session(db_engine) as session:
        return build_resource_graph(
            session,
            _principal(group_id),
            root_mfid,
            ResourceGraphQuery(**kwargs),
        )


def test_depth_one_returns_only_immediate_readable_neighbors(db_engine):
    group_id = _setup(db_engine)

    graph = _graph(db_engine, "sample-root", group_id)

    assert [(node.mfid, node.traversal_depth) for node in graph.nodes] == [
        ("sample-root", 0),
        ("sample-child", 1),
    ]
    assert len(graph.edges) == 1
    assert graph.edges[0].kind == "lineage"
    assert graph.edges[0].relationship_type == "is_derived_from"
    assert graph.has_more is True
    assert graph.nodes[1].has_more_relationships is True
    assert graph.nodes[0].expansion_availability.model_dump() == {
        "upstream_lineage": False,
        "downstream_lineage": False,
        "associations": False,
    }
    assert graph.nodes[1].expansion_availability.model_dump() == {
        "upstream_lineage": False,
        "downstream_lineage": True,
        "associations": True,
    }


def test_depth_zero_returns_root_and_expandability(db_engine):
    group_id = _setup(db_engine)

    graph = _graph(db_engine, "sample-root", group_id, depth=0)

    assert [node.mfid for node in graph.nodes] == ["sample-root"]
    assert graph.edges == []
    assert graph.returned_depth == 0
    assert graph.has_more is True
    assert graph.nodes[0].has_more_relationships is True
    assert graph.nodes[0].expansion_availability.model_dump() == {
        "upstream_lineage": False,
        "downstream_lineage": True,
        "associations": False,
    }


def test_depth_two_traverses_associations_and_untyped_lineage(db_engine):
    group_id = _setup(db_engine)

    graph = _graph(db_engine, "sample-root", group_id, depth=2)

    assert [(node.mfid, node.traversal_depth) for node in graph.nodes] == [
        ("sample-root", 0),
        ("sample-child", 1),
        ("dataset-root", 2),
        ("sample-grandchild", 2),
    ]
    assert {(edge.kind, edge.relationship_type) for edge in graph.edges} == {
        ("association", None),
        ("lineage", None),
        ("lineage", "is_derived_from"),
    }
    assert graph.has_more is True


def test_direction_controls_lineage_without_reversing_edges(db_engine):
    group_id = _setup(db_engine)

    upstream = _graph(
        db_engine,
        "sample-grandchild",
        group_id,
        direction=GraphDirection.UPSTREAM,
        relationship_kind=[GraphRelationshipKind.LINEAGE],
    )
    downstream = _graph(
        db_engine,
        "sample-grandchild",
        group_id,
        direction=GraphDirection.DOWNSTREAM,
        relationship_kind=[GraphRelationshipKind.LINEAGE],
    )

    assert [node.mfid for node in upstream.nodes] == [
        "sample-grandchild",
        "sample-child",
    ]
    assert upstream.edges[0].source_mfid == "sample-child"
    assert upstream.edges[0].target_mfid == "sample-grandchild"
    assert [node.mfid for node in downstream.nodes] == ["sample-grandchild"]


def test_relationship_type_filter_includes_unspecified(db_engine):
    group_id = _setup(db_engine)

    graph = _graph(
        db_engine,
        "sample-child",
        group_id,
        direction=GraphDirection.DOWNSTREAM,
        relationship_kind=[GraphRelationshipKind.LINEAGE],
        relationship_type=[GraphRelationshipTypeFilter.UNSPECIFIED],
    )

    assert [node.mfid for node in graph.nodes] == [
        "sample-child",
        "sample-grandchild",
    ]
    assert graph.edges[0].relationship_type is None
    assert graph.nodes[0].expansion_availability.model_dump() == {
        "upstream_lineage": False,
        "downstream_lineage": False,
        "associations": True,
    }
    assert graph.has_more is False


def test_association_only_graph_is_undirected(db_engine):
    group_id = _setup(db_engine)

    graph = _graph(
        db_engine,
        "sample-child",
        group_id,
        relationship_kind=[GraphRelationshipKind.ASSOCIATION],
    )

    assert [node.mfid for node in graph.nodes] == ["sample-child", "dataset-root"]
    assert graph.edges[0].kind == "association"
    assert graph.edges[0].directed is False
    assert graph.edges[0].source_mfid == "sample-child"
    assert graph.edges[0].target_mfid == "dataset-root"
    assert graph.nodes[0].expansion_availability.model_dump() == {
        "upstream_lineage": True,
        "downstream_lineage": True,
        "associations": False,
    }
    assert graph.nodes[1].expansion_availability.model_dump() == {
        "upstream_lineage": False,
        "downstream_lineage": True,
        "associations": False,
    }
    assert graph.has_more is False


def test_hidden_nodes_are_not_returned_or_traversed(db_engine):
    group_id = _setup(db_engine)

    graph = _graph(
        db_engine,
        "sample-root",
        group_id,
        depth=3,
        relationship_kind=[GraphRelationshipKind.LINEAGE],
    )

    assert {node.mfid for node in graph.nodes} == {
        "sample-root",
        "sample-child",
        "sample-grandchild",
    }
    assert all("hidden" not in edge.id for edge in graph.edges)
    root = next(node for node in graph.nodes if node.mfid == "sample-root")
    assert root.expansion_availability.model_dump() == {
        "upstream_lineage": False,
        "downstream_lineage": False,
        "associations": False,
    }


def test_inaccessible_or_unsupported_root_is_not_resolved(db_engine):
    group_id = _setup(db_engine)

    assert _graph(db_engine, "sample-hidden", group_id) is None
    assert _graph(db_engine, "missing", group_id) is None


def test_cycles_do_not_duplicate_nodes(db_engine):
    group_id = _setup(db_engine)
    with Session(db_engine) as session:
        root = session.exec(
            select(Sample).where(Sample.unique_id == "sample-root")
        ).one()
        grandchild = session.exec(
            select(Sample).where(Sample.unique_id == "sample-grandchild")
        ).one()
        session.add(SampleLink(
            parent_sample_id=grandchild.id,
            sample_id=root.id,
            relationship_type="is_derived_from",
        ))
        session.commit()

    graph = _graph(
        db_engine,
        "sample-root",
        group_id,
        depth=3,
        relationship_kind=[GraphRelationshipKind.LINEAGE],
    )

    assert [node.mfid for node in graph.nodes] == [
        "sample-root",
        "sample-child",
        "sample-grandchild",
    ]
    assert len(graph.edges) == 3
    assert len({edge.id for edge in graph.edges}) == 3


def test_self_links_are_not_returned(db_engine):
    group_id = _setup(db_engine)
    with Session(db_engine) as session:
        root = session.exec(
            select(Sample).where(Sample.unique_id == "sample-root")
        ).one()
        session.add(SampleLink(
            parent_sample_id=root.id,
            sample_id=root.id,
            relationship_type="is_derived_from",
        ))
        session.commit()

    graph = _graph(
        db_engine,
        "sample-root",
        group_id,
        relationship_kind=[GraphRelationshipKind.LINEAGE],
    )

    assert all(edge.source_mfid != edge.target_mfid for edge in graph.edges)


def test_node_limit_returns_deterministic_partial_graph(db_engine, monkeypatch):
    group_id = _setup(db_engine)
    monkeypatch.setattr(resource_graph_service, "GRAPH_NODE_LIMIT", 2)

    graph = _graph(db_engine, "sample-root", group_id, depth=2)

    assert [node.mfid for node in graph.nodes] == ["sample-root", "sample-child"]
    assert graph.truncated is True
    assert graph.truncation_reasons == ["node_limit"]
    assert graph.has_more is True
    assert graph.nodes[1].has_more_relationships is True


def test_edge_limit_returns_deterministic_partial_graph(db_engine, monkeypatch):
    group_id = _setup(db_engine)
    monkeypatch.setattr(resource_graph_service, "GRAPH_EDGE_LIMIT", 1)

    graph = _graph(db_engine, "sample-child", group_id)

    assert [node.mfid for node in graph.nodes] == ["sample-child", "dataset-root"]
    assert [edge.kind for edge in graph.edges] == ["association"]
    assert graph.truncated is True
    assert graph.truncation_reasons == ["edge_limit"]
    assert graph.has_more is True
    assert graph.nodes[0].has_more_relationships is True
    assert graph.nodes[0].expansion_availability.model_dump() == {
        "upstream_lineage": True,
        "downstream_lineage": True,
        "associations": False,
    }


def test_query_rejects_relationship_types_without_lineage():
    try:
        ResourceGraphQuery(
            relationship_kind=[GraphRelationshipKind.ASSOCIATION],
            relationship_type=[GraphRelationshipTypeFilter.IS_DERIVED_FROM],
        )
    except ValueError as exc:
        assert "relationship_type requires relationship_kind=lineage" in str(exc)
    else:
        raise AssertionError("invalid relationship filter combination was accepted")


def test_openapi_publishes_typed_resource_graph_contract():
    schema = app.openapi()
    operation = schema["paths"]["/resources/{resource_mfid}/graph"]["get"]
    parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}

    assert set(parameters) == {
        "Crucible-Privilege-Mode",
        "resource_mfid",
        "depth",
        "direction",
        "relationship_kind",
        "relationship_type",
    }
    response = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response["$ref"] == "#/components/schemas/ResourceGraphResponse"
    assert "missing, deleted, inaccessible" in operation["responses"]["404"]["description"]
    assert "SampleGraphNode" in schema["components"]["schemas"]
    assert "DatasetGraphNode" in schema["components"]["schemas"]
    assert "LineageGraphEdge" in schema["components"]["schemas"]
    assert "AssociationGraphEdge" in schema["components"]["schemas"]
    assert set(schema["components"]["schemas"]["SampleGraphNode"]["required"]) == {
        "mfid",
        "name",
        "traversal_depth",
        "has_more_relationships",
        "expansion_availability",
        "project",
        "resource_type",
        "sample_type",
    }
    assert set(
        schema["components"]["schemas"]["GraphExpansionAvailability"]["required"]
    ) == {
        "upstream_lineage",
        "downstream_lineage",
        "associations",
    }


def test_resource_graph_route_returns_graph(db_engine):
    group_id = _setup(db_engine)
    graph = read_resource_graph(
        "sample-root",
        _principal(group_id),
        ResourceGraphQuery(
            depth=2,
            relationship_kind=[GraphRelationshipKind.LINEAGE],
            relationship_type=[GraphRelationshipTypeFilter.IS_DERIVED_FROM],
        ),
    )

    assert [node.mfid for node in graph.nodes] == [
        "sample-root",
        "sample-child",
    ]
    assert graph.edges[0].relationship_type == "is_derived_from"


def test_resource_graph_route_hides_inaccessible_root(db_engine):
    group_id = _setup(db_engine)

    with pytest.raises(HTTPException) as exc_info:
        read_resource_graph(
            "sample-hidden",
            _principal(group_id),
            ResourceGraphQuery(),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Resource not found"
