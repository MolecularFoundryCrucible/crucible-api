import pytest
from sqlmodel import Session, select

from app.tables import AccessGroup, Dataset, DatasetLink, Project, Resource, ResourceACL, Sample, SampleLink
from app.utils.permissions import Role

USER = "lineage-user"
KINDS = {
    "datasets": (Dataset, DatasetLink, DatasetLink.parent_dataset_id, DatasetLink.child_dataset_id, "dataset_name"),
    "samples": (Sample, SampleLink, SampleLink.parent_sample_id, SampleLink.sample_id, "sample_name"),
}


@pytest.fixture(params=sorted(KINDS))
def lineage(request, client, db_engine, auth_as):
    kind = request.param
    model, link, parent_column, child_column, name_field = KINDS[kind]
    with Session(db_engine) as session:
        group = AccessGroup(group_name="lineage-editors")
        hidden_group = AccessGroup(group_name="lineage-hidden")
        project = Project(project_id="lineage-project", organization="LBNL", project_lead_orcid=USER)
        session.add_all([group, hidden_group, project])
        session.commit()
        for row in (group, hidden_group, project):
            session.refresh(row)
        records = {name: model(unique_id=name, project_mfid=project.unique_id, **{name_field: name}) for name in ("a", "b", "c", "hidden")}
        session.add_all(records.values())
        session.commit()
        for name, record in records.items():
            session.refresh(record)
            session.add(Resource(mfid=name, resource_type=kind[:-1]))
            session.add(ResourceACL(mfid=name, accessgroup_id=(hidden_group if name == "hidden" else group).id, read=True, write=True, permission=Role.EDITOR.db_value))
        session.commit()
        ids = {name: record.id for name, record in records.items()}
        group_id = group.id
    auth_as(USER, groups=[("lineage-editors", group_id, Role.EDITOR)])

    def connect(parent, child):
        return client.post(f"/{kind}/{parent}/children/{child}")

    def insert(parent, child):
        with Session(db_engine) as session:
            session.add(link(**{parent_column.key: ids[parent], child_column.key: ids[child]}))
            session.commit()

    def links():
        with Session(db_engine) as session:
            return len(session.exec(select(link)).all())

    return connect, insert, links


def test_self_links_are_rejected(lineage):
    connect, _, links = lineage
    response = connect("a", "a")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "self_link"
    assert links() == 0


def test_reversing_a_link_is_rejected(lineage):
    connect, _, links = lineage
    assert connect("a", "b").status_code == 200
    response = connect("b", "a")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "lineage_cycle"
    assert links() == 1


def test_longer_cycles_are_rejected(lineage):
    connect, _, links = lineage
    assert connect("a", "b").status_code == 200
    assert connect("b", "c").status_code == 200
    assert connect("c", "a").status_code == 409
    assert connect("a", "c").status_code == 200
    assert links() == 3


def test_cycles_through_unreadable_records_are_rejected_without_naming_them(lineage):
    connect, insert, _ = lineage
    insert("a", "hidden")
    insert("hidden", "b")
    response = connect("b", "a")
    assert response.status_code == 409
    assert "hidden" not in response.text


def test_relinking_an_existing_link_still_succeeds(lineage):
    connect, insert, links = lineage
    insert("a", "b")
    insert("b", "a")
    assert connect("a", "b").status_code == 200
    assert links() == 2
