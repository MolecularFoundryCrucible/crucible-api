"""Typed parent/child links between datasets, and between samples.

relationship_type describes the child relative to the parent, so a link
(parent=A, child=B, "is_part_of") reads "B is_part_of A". It is nullable:
links created before typing existed, and callers who do not care, leave it
unset.
"""
from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup,
    Dataset,
    DatasetLink,
    Project,
    Resource,
    ResourceACL,
    Sample,
    SampleLink,
)
from app.utils.permissions import Role


GROUP_NAME = "link-type-editors"
USER = "link-type-user"


def _setup(db_engine):
    """Two datasets and two samples in one project, all writable by the caller.

    No links are created here; each test makes the links it needs.
    """
    with Session(db_engine) as session:
        group = AccessGroup(group_name=GROUP_NAME)
        project = Project(
            project_id="link-type-project",
            organization="LBNL",
            project_lead_orcid=USER,
        )
        session.add_all([group, project])
        session.commit()
        session.refresh(group)
        session.refresh(project)

        entities = [
            Dataset(unique_id="ds_parent", dataset_name="parent dataset",
                    project_mfid=project.unique_id),
            Dataset(unique_id="ds_child", dataset_name="child dataset",
                    project_mfid=project.unique_id),
            Sample(unique_id="samp_parent", sample_name="parent sample",
                   project_mfid=project.unique_id),
            Sample(unique_id="samp_child", sample_name="child sample",
                   project_mfid=project.unique_id),
        ]
        session.add_all(entities)
        session.commit()

        rows = [Resource(mfid=project.unique_id, resource_type="project")]
        rows.append(ResourceACL(mfid=project.unique_id, accessgroup_id=group.id,
                                read=True, write=True, permission=Role.EDITOR.db_value))
        for entity in entities:
            resource_type = "dataset" if isinstance(entity, Dataset) else "sample"
            rows.append(Resource(mfid=entity.unique_id, resource_type=resource_type))
            rows.append(ResourceACL(mfid=entity.unique_id, accessgroup_id=group.id,
                                    read=True, write=True, permission=Role.EDITOR.db_value))
        session.add_all(rows)
        session.commit()
        return project.unique_id, group.id


def _authorize(auth_as, group_id):
    auth_as(USER, groups=[(GROUP_NAME, group_id, Role.EDITOR)])


def _dataset_link(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(DatasetLink)).one()


def _sample_link(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(SampleLink)).one()


# --- writing -------------------------------------------------------------


def test_dataset_link_stores_relationship_type(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    response = client.post("/datasets/ds_parent/children/ds_child",
                           params={"relationship_type": "is_part_of"})

    assert response.status_code == 200, response.text
    assert response.json()["relationship_type"] == "is_part_of"
    assert _dataset_link(db_engine).relationship_type == "is_part_of"


def test_sample_link_stores_relationship_type(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    response = client.post("/samples/samp_parent/children/samp_child",
                           params={"relationship_type": "is_derived_from"})

    assert response.status_code == 200, response.text
    assert response.json()["relationship_type"] == "is_derived_from"
    assert _sample_link(db_engine).relationship_type == "is_derived_from"


def test_link_without_relationship_type_is_untyped(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    datasets = client.post("/datasets/ds_parent/children/ds_child")
    samples = client.post("/samples/samp_parent/children/samp_child")

    assert datasets.status_code == 200, datasets.text
    assert samples.status_code == 200, samples.text
    assert _dataset_link(db_engine).relationship_type is None
    assert _sample_link(db_engine).relationship_type is None


def test_relink_with_new_relationship_type_overwrites(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    client.post("/datasets/ds_parent/children/ds_child",
                params={"relationship_type": "is_part_of"})
    response = client.post("/datasets/ds_parent/children/ds_child",
                           params={"relationship_type": "is_derived_from"})

    assert response.status_code == 200, response.text
    assert _dataset_link(db_engine).relationship_type == "is_derived_from"


def test_relink_without_relationship_type_preserves_existing(client, db_engine, auth_as):
    """A bare re-POST means "ensure this link exists", not "clear its type"."""
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    client.post("/datasets/ds_parent/children/ds_child",
                params={"relationship_type": "is_part_of"})
    response = client.post("/datasets/ds_parent/children/ds_child")

    assert response.status_code == 200, response.text
    assert _dataset_link(db_engine).relationship_type == "is_part_of"


def test_unknown_relationship_type_is_rejected(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    datasets = client.post("/datasets/ds_parent/children/ds_child",
                           params={"relationship_type": "is_a_friend_of"})
    samples = client.post("/samples/samp_parent/children/samp_child",
                          params={"relationship_type": "is_a_friend_of"})

    assert datasets.status_code == 422, datasets.text
    assert samples.status_code == 422, samples.text
    with Session(db_engine) as session:
        assert session.exec(select(DatasetLink)).all() == []
        assert session.exec(select(SampleLink)).all() == []


# --- reading -------------------------------------------------------------


def test_include_links_surfaces_relationship_type(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)
    client.post("/datasets/ds_parent/children/ds_child",
                params={"relationship_type": "is_part_of"})

    child = client.get("/datasets/ds_child", params={"include_links": True})
    parent = client.get("/datasets/ds_parent", params={"include_links": True})

    assert child.status_code == 200, child.text
    assert parent.status_code == 200, parent.text
    child_links = [l for l in child.json()["links"] if l["direction"] == "source"]
    parent_links = [l for l in parent.json()["links"] if l["direction"] == "target"]
    assert [l["relationship_type"] for l in child_links] == ["is_part_of"]
    assert [l["relationship_type"] for l in parent_links] == ["is_part_of"]


def test_include_links_reports_untyped_links_as_null(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)
    client.post("/samples/samp_parent/children/samp_child")

    response = client.get("/samples/samp_child", params={"include_links": True})

    assert response.status_code == 200, response.text
    parents = [l for l in response.json()["links"] if l["direction"] == "source"]
    assert [l["relationship_type"] for l in parents] == [None]


def test_graph_edges_carry_relationship_type(client, db_engine, auth_as):
    project_mfid, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)
    client.post("/datasets/ds_parent/children/ds_child",
                params={"relationship_type": "is_derived_from"})
    client.post("/samples/samp_parent/children/samp_child",
                params={"relationship_type": "is_part_of"})

    dataset_graph = client.get("/resources/ds_child/graph")
    sample_graph = client.get("/resources/samp_child/graph")

    for response in (dataset_graph, sample_graph):
        assert response.status_code == 200, response.text

    assert [e["relationship_type"] for e in dataset_graph.json()["edges"]] == ["is_derived_from"]
    assert [e["relationship_type"] for e in sample_graph.json()["edges"]] == ["is_part_of"]


# --- filtering -----------------------------------------------------------


def test_children_filter_by_relationship_type(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)
    client.post("/datasets/ds_parent/children/ds_child",
                params={"relationship_type": "is_part_of"})

    matching = client.get("/datasets/ds_parent/children",
                          params={"relationship_type": "is_part_of"})
    other = client.get("/datasets/ds_parent/children",
                       params={"relationship_type": "is_derived_from"})
    unfiltered = client.get("/datasets/ds_parent/children")

    assert [i["unique_id"] for i in matching.json()["items"]] == ["ds_child"]
    assert other.json()["items"] == []
    assert [i["unique_id"] for i in unfiltered.json()["items"]] == ["ds_child"]


def test_parents_filter_by_relationship_type(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)
    client.post("/samples/samp_parent/children/samp_child",
                params={"relationship_type": "is_derived_from"})

    matching = client.get("/samples/samp_child/parents",
                          params={"relationship_type": "is_derived_from"})
    other = client.get("/samples/samp_child/parents",
                       params={"relationship_type": "is_part_of"})

    assert [i["unique_id"] for i in matching.json()["items"]] == ["samp_parent"]
    assert other.json()["items"] == []


def test_filter_excludes_untyped_links(client, db_engine, auth_as):
    """An explicit filter means "this kind", which an unspecified link is not."""
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)
    client.post("/datasets/ds_parent/children/ds_child")

    filtered = client.get("/datasets/ds_parent/children",
                          params={"relationship_type": "is_part_of"})
    unfiltered = client.get("/datasets/ds_parent/children")

    assert filtered.json()["items"] == []
    assert [i["unique_id"] for i in unfiltered.json()["items"]] == ["ds_child"]


def test_unknown_filter_value_is_rejected(client, db_engine, auth_as):
    _, group_id = _setup(db_engine)
    _authorize(auth_as, group_id)

    response = client.get("/datasets/ds_parent/children",
                          params={"relationship_type": "is_a_friend_of"})

    assert response.status_code == 422, response.text
