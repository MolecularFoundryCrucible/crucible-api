from sqlmodel import Session

from app.models.core_models import (
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
from app.utils.permissions import Role


def _setup(db_engine):
    with Session(db_engine) as session:
        caller_group = AccessGroup(group_name="relationship-readers")
        other_group = AccessGroup(group_name="relationship-private")
        project = Project(
            project_id="relationship-project",
            organization="LBNL",
            project_lead_orcid="relationship-user",
        )
        session.add_all([caller_group, other_group, project])
        session.commit()
        session.refresh(caller_group)
        session.refresh(other_group)
        session.refresh(project)

        accessible_sample = Sample(
            unique_id="sample_accessible",
            sample_name="accessible sample",
            project_mfid=project.unique_id,
        )
        private_sample = Sample(
            unique_id="sample_private",
            sample_name="private sample",
            project_mfid=project.unique_id,
        )
        accessible_dataset = Dataset(
            unique_id="dataset_accessible",
            dataset_name="accessible dataset",
            project_mfid=project.unique_id,
        )
        private_dataset = Dataset(
            unique_id="dataset_private",
            dataset_name="private dataset",
            project_mfid=project.unique_id,
        )
        session.add_all([
            accessible_sample,
            private_sample,
            accessible_dataset,
            private_dataset,
        ])
        session.commit()
        session.refresh(accessible_sample)
        session.refresh(private_sample)
        session.refresh(accessible_dataset)
        session.refresh(private_dataset)

        session.add_all([
            Resource(mfid=project.unique_id, resource_type="project"),
            Resource(mfid=accessible_sample.unique_id, resource_type="sample"),
            Resource(mfid=private_sample.unique_id, resource_type="sample"),
            Resource(mfid=accessible_dataset.unique_id, resource_type="dataset"),
            Resource(mfid=private_dataset.unique_id, resource_type="dataset"),
            ResourceACL(
                mfid=project.unique_id,
                accessgroup_id=caller_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=accessible_sample.unique_id,
                accessgroup_id=caller_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=private_sample.unique_id,
                accessgroup_id=other_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=accessible_dataset.unique_id,
                accessgroup_id=caller_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            ResourceACL(
                mfid=private_dataset.unique_id,
                accessgroup_id=other_group.id,
                read=True,
                write=False,
                permission=Role.VIEWER.db_value,
            ),
            SampleLink(
                parent_sample_id=private_sample.id,
                sample_id=accessible_sample.id,
            ),
            DatasetLink(
                parent_dataset_id=private_dataset.id,
                child_dataset_id=accessible_dataset.id,
            ),
            DatasetSampleLink(
                dataset_id=accessible_dataset.id,
                sample_id=private_sample.id,
            ),
            DatasetSampleLink(
                dataset_id=private_dataset.id,
                sample_id=accessible_sample.id,
            ),
        ])
        session.commit()
        return project.unique_id, caller_group.id


def _authorize(auth_as, group_id):
    auth_as("relationship-user", groups=[("relationship-readers", group_id, Role.VIEWER)])


def test_sample_lineage_requires_access_to_starting_sample(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    response = client.get("/samples/sample_private/children")

    assert response.status_code == 403, response.text


def test_sample_lineage_omits_inaccessible_relatives(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    response = client.get("/samples/sample_accessible/parents")

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_sample_dataset_relationships_filter_both_directions(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    samples = client.get("/datasets/dataset_accessible/samples")
    datasets = client.get("/samples/sample_accessible/datasets")

    assert samples.status_code == 200, samples.text
    assert samples.json()["items"] == []
    assert datasets.status_code == 200, datasets.text
    assert datasets.json() == []


def test_sample_graphs_omit_inaccessible_nodes(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    sample_graph = client.get("/samples/sample_accessible/sample_graph")
    sample_graph_cte = client.get("/samples/sample_accessible/sample_graph_cte")
    project_graph = client.get(f"/projects/{project_mfid}/sample_graph")
    entity_graph = client.get(f"/projects/{project_mfid}/entity_graph")

    assert sample_graph.status_code == 200, sample_graph.text
    assert sample_graph_cte.status_code == 200, sample_graph_cte.text
    assert project_graph.status_code == 200, project_graph.text
    assert entity_graph.status_code == 200, entity_graph.text
    assert {node["id"] for node in sample_graph.json()["nodes"]} == {"sample_accessible"}
    assert {node["id"] for node in sample_graph_cte.json()["nodes"]} == {"sample_accessible"}
    assert {node["id"] for node in project_graph.json()["nodes"]} == {"sample_accessible"}
    assert "sample_private" not in {node["id"] for node in entity_graph.json()["nodes"]}


def test_dataset_graphs_do_not_leak_inaccessible_parent_ids(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    dataset_graph = client.get("/datasets/dataset_accessible/dataset_graph")
    project_graph = client.get(f"/projects/{project_mfid}/dataset_graph")

    assert dataset_graph.status_code == 200, dataset_graph.text
    assert project_graph.status_code == 200, project_graph.text
    assert {node["id"] for node in dataset_graph.json()["nodes"]} == {"dataset_accessible"}
    assert {node["id"] for node in project_graph.json()["nodes"]} == {"dataset_accessible"}
