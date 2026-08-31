import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

import app.api.routes.entities as entities_routes
from app.models.core_models import (
    AccessGroup,
    Dataset,
    DatasetLink,
    DatasetSampleLink,
    DeletionRequest,
    Project,
    Resource,
    ResourceACL,
    Sample,
    SampleLink,
)
from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role


def _setup(db_engine):
    with Session(db_engine) as session:
        reader_group = AccessGroup(group_name="graph-readers")
        private_group = AccessGroup(group_name="graph-private")
        project = Project(
            unique_id="0tkvpezyr5w3b000stgq42knzr",
            project_id="project-graph-slug",
            organization="LBNL",
            project_lead_orcid="graph-user",
        )
        session.add_all([reader_group, private_group, project])
        session.commit()
        session.refresh(reader_group)
        session.refresh(private_group)

        visible_sample_root = Sample(
            unique_id="sample-visible-root",
            sample_name="visible sample root",
            project_mfid=project.unique_id,
        )
        private_sample = Sample(
            unique_id="sample-private",
            sample_name="private sample",
            project_mfid=project.unique_id,
        )
        visible_sample_beyond_private = Sample(
            unique_id="sample-visible-beyond-private",
            sample_name="visible sample beyond private",
            project_mfid=project.unique_id,
        )
        deleted_sample = Sample(
            unique_id="sample-deleted",
            sample_name="deleted sample",
            project_mfid=project.unique_id,
        )
        visible_dataset_root = Dataset(
            unique_id="dataset-visible-root",
            dataset_name="visible dataset root",
            project_mfid=project.unique_id,
        )
        private_dataset = Dataset(
            unique_id="dataset-private",
            dataset_name="private dataset",
            project_mfid=project.unique_id,
        )
        visible_dataset_beyond_private = Dataset(
            unique_id="dataset-visible-beyond-private",
            dataset_name="visible dataset beyond private",
            project_mfid=project.unique_id,
        )
        session.add_all([
            visible_sample_root,
            private_sample,
            visible_sample_beyond_private,
            deleted_sample,
            visible_dataset_root,
            private_dataset,
            visible_dataset_beyond_private,
        ])
        session.commit()
        for entity in (
            visible_sample_root,
            private_sample,
            visible_sample_beyond_private,
            deleted_sample,
            visible_dataset_root,
            private_dataset,
            visible_dataset_beyond_private,
        ):
            session.refresh(entity)

        session.add_all([
            Resource(mfid=visible_sample_root.unique_id, resource_type="sample"),
            Resource(mfid=private_sample.unique_id, resource_type="sample"),
            Resource(mfid=visible_sample_beyond_private.unique_id, resource_type="sample"),
            Resource(mfid=deleted_sample.unique_id, resource_type="sample"),
            Resource(mfid=visible_dataset_root.unique_id, resource_type="dataset"),
            Resource(mfid=private_dataset.unique_id, resource_type="dataset"),
            Resource(mfid=visible_dataset_beyond_private.unique_id, resource_type="dataset"),
        ])
        for entity in (
            visible_sample_root,
            visible_sample_beyond_private,
            deleted_sample,
            visible_dataset_root,
            visible_dataset_beyond_private,
        ):
            session.add(ResourceACL(
                mfid=entity.unique_id,
                accessgroup_id=reader_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ))
        for entity in (private_sample, private_dataset):
            session.add(ResourceACL(
                mfid=entity.unique_id,
                accessgroup_id=private_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ))
        session.add_all([
            SampleLink(
                parent_sample_id=visible_sample_root.id,
                sample_id=private_sample.id,
            ),
            SampleLink(
                parent_sample_id=private_sample.id,
                sample_id=visible_sample_beyond_private.id,
            ),
            SampleLink(
                parent_sample_id=visible_sample_root.id,
                sample_id=deleted_sample.id,
            ),
            DatasetLink(
                parent_dataset_id=visible_dataset_root.id,
                child_dataset_id=private_dataset.id,
            ),
            DatasetLink(
                parent_dataset_id=private_dataset.id,
                child_dataset_id=visible_dataset_beyond_private.id,
            ),
            DatasetSampleLink(
                sample_id=visible_sample_root.id,
                dataset_id=private_dataset.id,
            ),
            DatasetSampleLink(
                sample_id=private_sample.id,
                dataset_id=visible_dataset_root.id,
            ),
            DeletionRequest(
                resource_id=deleted_sample.unique_id,
                requester_id="graph-user",
                status="pending",
            ),
        ])
        session.commit()
        return reader_group.id


def _reader(group_id: int) -> Principal:
    return Principal(
        user_id="graph-user",
        groups=(GroupMembership(
            id=group_id,
            group_name="graph-readers",
            role=Role.VIEWER,
        ),),
    )


def test_entity_graph_does_not_traverse_private_sample_bridge(
    db_engine,
    monkeypatch,
):
    group_id = _setup(db_engine)
    monkeypatch.setattr(entities_routes, "engine", db_engine)

    graph = entities_routes.entity_graph_cte(
        "sample-visible-root",
        _reader(group_id),
        recursive=True,
    )

    assert {node["id"] for node in graph["nodes"]} == {"sample-visible-root"}
    assert graph["edges"] == []


def test_entity_graph_does_not_traverse_private_dataset_bridge(
    db_engine,
    monkeypatch,
):
    group_id = _setup(db_engine)
    monkeypatch.setattr(entities_routes, "engine", db_engine)

    graph = entities_routes.entity_graph_cte(
        "dataset-visible-root",
        _reader(group_id),
        recursive=True,
    )

    assert {node["id"] for node in graph["nodes"]} == {"dataset-visible-root"}
    assert graph["edges"] == []


def test_project_graph_returns_only_visible_active_resources(
    db_engine,
    monkeypatch,
):
    group_id = _setup(db_engine)
    monkeypatch.setattr(entities_routes, "engine", db_engine)

    graph = entities_routes.project_graph(
        "project-graph-slug",
        _reader(group_id),
    )

    assert {node["id"] for node in graph["nodes"]} == {
        "sample-visible-root",
        "sample-visible-beyond-private",
        "dataset-visible-root",
        "dataset-visible-beyond-private",
    }
    assert graph["edges"] == []


def test_entity_graph_hides_inaccessible_root(db_engine, monkeypatch):
    group_id = _setup(db_engine)
    monkeypatch.setattr(entities_routes, "engine", db_engine)

    with pytest.raises(HTTPException) as exc:
        entities_routes.entity_graph_cte(
            "sample-private",
            _reader(group_id),
            recursive=True,
        )

    assert exc.value.status_code == 404


def test_recursive_entity_graph_handles_visible_lineage_cycle(
    db_engine,
    monkeypatch,
):
    group_id = _setup(db_engine)
    monkeypatch.setattr(entities_routes, "engine", db_engine)
    with Session(db_engine) as session:
        root = session.exec(
            select(Sample).where(Sample.unique_id == "sample-visible-root")
        ).one()
        other = session.exec(
            select(Sample).where(
                Sample.unique_id == "sample-visible-beyond-private"
            )
        ).one()
        session.add_all([
            SampleLink(parent_sample_id=root.id, sample_id=other.id),
            SampleLink(parent_sample_id=other.id, sample_id=root.id),
        ])
        session.commit()

    graph = entities_routes.entity_graph_cte(
        "sample-visible-root",
        _reader(group_id),
        recursive=True,
    )

    assert {node["id"] for node in graph["nodes"]} == {
        "sample-visible-root",
        "sample-visible-beyond-private",
    }
    assert len(graph["edges"]) == 2


def test_platform_admin_graph_still_excludes_pending_deletions(
    db_engine,
    monkeypatch,
):
    _setup(db_engine)
    monkeypatch.setattr(entities_routes, "engine", db_engine)
    principal = Principal(
        user_id="platform-admin",
        platform_role=PlatformRole.ADMIN,
    )

    graph = entities_routes.project_graph("0tkvpezyr5w3b000stgq42knzr", principal)

    assert "sample-private" in {node["id"] for node in graph["nodes"]}
    assert "dataset-private" in {node["id"] for node in graph["nodes"]}
    assert "sample-deleted" not in {node["id"] for node in graph["nodes"]}
