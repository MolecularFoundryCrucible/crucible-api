import pytest
from sqlmodel import Session

from app.tables import AccessGroup, Dataset, DeletionRequest, Instrument, Project, Resource, ResourceACL, Sample, User
from app.utils.permissions import Role

ME = "0000-0004-0000-0101"
OTHER = "0000-0004-0000-0102"


def _resource(session, mfid, resource_type, readable=True, **fields):
    session.add(Resource(mfid=mfid, resource_type=resource_type))
    model = {"dataset": Dataset, "sample": Sample, "project": Project, "instrument": Instrument}[resource_type]
    session.add(model(unique_id=mfid, **fields))
    if readable:
        session.add(ResourceACL(mfid=mfid, accessgroup_id=1, read=True, write=False, permission=Role.VIEWER.db_value))


def _project(session, mfid, slug):
    _resource(session, mfid, "project", readable=False, project_id=slug, title=slug, organization="MF", status="active", project_lead_orcid=OTHER, project_lead_email="lead@example.org")


@pytest.fixture
def me(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(AccessGroup(id=1, group_name="test_group"))
        session.add(User(first_name="Me", last_name="Caller", email="me@example.org", unique_id=ME, username="caller"))
        session.add(User(first_name="Ot", last_name="Her", email="other@example.org", unique_id=OTHER, username="other"))
        for mfid in ("ds1", "ds2", "ds3", "pending"):
            _resource(session, mfid, "dataset", dataset_name=mfid)
        _resource(session, "hidden", "dataset", readable=False, dataset_name="hidden")
        session.add(DeletionRequest(resource_id="pending", resource_type="dataset", requester_id=ME, status="pending", reason="test"))
        for mfid in ("sm1", "sm2"):
            _resource(session, mfid, "sample", sample_name=mfid)
        _project(session, "pr1", "proj-one")
        _project(session, "pr2", "proj-two")
        _resource(session, "in1", "instrument", instrument_id="tem-1", instrument_name="TEM", owner="facility", location="B67")
        _resource(session, "in2", "instrument", instrument_id="sem-1", instrument_name="SEM", owner="facility", location="B67", status="decommissioned")
        session.commit()
    auth_as(ME, groups=[("test_group", 1, Role.VIEWER)])
    return client


def _mfids(response):
    assert response.status_code == 200, response.text
    return sorted(item["unique_id"] for item in response.json()["items"])


@pytest.mark.parametrize("path", ["/datasets", "/samples"])
def test_mfid_filter_returns_only_readable_active_matches(me, path):
    wanted = ["ds1", "ds2", "hidden", "pending", "missing"] if path == "/datasets" else ["sm1", "sm2", "missing"]
    response = me.get(path, params={"mfid": wanted})
    assert _mfids(response) == (["ds1", "ds2"] if path == "/datasets" else ["sm1", "sm2"])
    assert "Deprecation" not in response.headers


def test_unique_id_is_a_deprecated_alias(me):
    response = me.get("/datasets", params={"unique_id": ["ds1", "ds3"]})
    assert _mfids(response) == ["ds1", "ds3"]
    assert response.headers["Deprecation"].startswith("@")
    assert me.get("/datasets", params={"unique_id": "ds1", "mfid": "ds2"}).status_code == 422


@pytest.mark.parametrize("path", ["/datasets", "/samples", "/projects", "/instruments", "/users"])
def test_identifier_filters_are_capped(me, path):
    name = "unique_id" if path == "/users" else "mfid"
    assert me.get(path, params={name: [f"x{i}" for i in range(101)]}).status_code == 422


def test_project_mfid_lookup_finds_projects_the_caller_is_not_a_member_of(me):
    assert _mfids(me.get("/projects")) == []
    assert _mfids(me.get("/projects", params={"mfid": ["pr1", "pr2", "missing"]})) == ["pr1", "pr2"]


def test_instrument_mfid_lookup_searches_every_status(me):
    assert _mfids(me.get("/instruments")) == ["in1"]
    assert _mfids(me.get("/instruments", params={"mfid": ["in1", "in2"]})) == ["in1", "in2"]
    assert _mfids(me.get("/instruments", params={"mfid": ["in1", "in2"], "status": "active"})) == ["in1"]


def test_user_exact_filters_are_repeatable(me):
    assert _mfids(me.get("/users", params={"unique_id": [ME, OTHER]})) == [ME, OTHER]
    assert _mfids(me.get("/users", params={"username": ["CALLER", "other"]})) == [ME, OTHER]
    assert _mfids(me.get("/users", params={"email": "Other@example.org"})) == [OTHER]


def test_user_resolve_is_deprecated(me):
    response = me.post("/users/resolve", json={"orcids": [OTHER]})
    assert response.status_code == 200
    assert list(response.json()) == [OTHER]
    assert response.headers["Deprecation"].startswith("@")
