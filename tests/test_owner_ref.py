"""owner_ref: one user reference in any form, on create bodies, ownership transfer and the repeatable owner filter."""
from sqlmodel import Session, select

from app.tables import User
from tests.test_instrument_members import _as, _user

ALICE = "0000-0004-0000-0001"
BOB = "0000-0004-0000-0002"
CAROL = "0000-0004-0000-0003"


def _people(db_engine):
    with Session(db_engine) as session:
        for unique_id, username in ((ALICE, "alice"), (BOB, "bob"), (CAROL, "carol")):
            _user(session, unique_id)
            session.exec(select(User).where(User.unique_id == unique_id)).one().username = username
        session.exec(select(User).where(User.unique_id == ALICE)).one().platform_role = "admin"
        session.commit()


def _dataset(client, name, owner_ref):
    resp = client.post("/datasets", json={"dataset_name": name, "owner_ref": owner_ref})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_create_bodies_accept_owner_ref_in_any_form(client, db_engine, auth_as):
    _people(db_engine)
    _as(auth_as, db_engine, ALICE)
    assert _dataset(client, "by username", "bob")["owner_orcid"] == BOB
    assert _dataset(client, "by id", CAROL)["owner_orcid"] == CAROL
    sample = client.post("/samples", json={"sample_name": "s", "owner_ref": "bob"})
    assert sample.status_code == 200 and sample.json()["owner_orcid"] == BOB
    instrument = client.post("/instruments", json={"instrument_id": "own-ref", "instrument_name": "I", "location": "b30", "owner_ref": "carol"})
    assert instrument.status_code == 200 and instrument.json()["owner_orcid"] == CAROL
    project = client.post("/projects", json={"project_id": "own-ref", "organization": "MF", "owner_ref": "bob"})
    assert project.status_code == 200, project.text
    assert project.json()["project_lead_orcid"] == BOB


def test_deprecated_owner_fields_still_work_but_not_together_with_owner_ref(client, db_engine, auth_as):
    _people(db_engine)
    _as(auth_as, db_engine, ALICE)
    assert client.post("/datasets", json={"dataset_name": "old", "owner": "bob"}).json()["owner_orcid"] == BOB
    assert client.post("/datasets", json={"dataset_name": "both", "owner_ref": "bob", "owner": "carol"}).status_code == 422
    assert client.post("/projects", json={"project_id": "both-p", "organization": "MF", "owner_ref": "bob", "project_lead": "carol"}).status_code == 422


def test_owner_filter_is_repeatable_with_or(client, db_engine, auth_as):
    _people(db_engine)
    _as(auth_as, db_engine, ALICE)
    ids = {owner: _dataset(client, f"d-{owner}", owner)["unique_id"] for owner in ("alice", "bob", "carol")}

    def owned(*refs):
        resp = client.get("/datasets", params=[("owner_ref", ref) for ref in refs])
        assert resp.status_code == 200, resp.text
        return {item["unique_id"] for item in resp.json()["items"]}

    assert owned("bob") == {ids["bob"]}
    assert owned("bob", CAROL) == {ids["bob"], ids["carol"]}
    assert owned("nobody-like-this") == set()
    assert client.get("/datasets", params={"owner_id": "bob"}).json()["items"][0]["unique_id"] == ids["bob"]
    assert client.get("/datasets", params=[("owner_ref", "bob"), ("owner_is_null", "true")]).status_code == 422
    facets = client.get("/datasets/facets", params=[("field", "owner"), ("owner_ref", "bob"), ("owner_ref", "carol")])
    assert facets.status_code == 200, facets.text
    assert sum(bucket["count"] for bucket in facets.json()["items"]) == 2


def test_owner_filter_on_instruments_and_projects(client, db_engine, auth_as):
    _people(db_engine)
    _as(auth_as, db_engine, ALICE)
    instrument = client.post("/instruments", json={"instrument_id": "inst-bob", "instrument_name": "I", "location": "b30", "owner_ref": "bob"}).json()["unique_id"]
    project = client.post("/projects", json={"project_id": "proj-carol", "organization": "MF", "owner_ref": "carol"}).json()["unique_id"]
    assert [i["unique_id"] for i in client.get("/instruments", params={"owner_ref": "bob"}).json()["items"]] == [instrument]
    assert [p["unique_id"] for p in client.get("/projects", params=[("owner_ref", "carol"), ("owner_ref", "bob")]).json()["items"]] == [project]


def test_transfer_takes_new_owner_ref(client, db_engine, auth_as):
    _people(db_engine)
    _as(auth_as, db_engine, ALICE)
    dataset = _dataset(client, "moving", "alice")["unique_id"]
    resp = client.post(f"/resources/{dataset}/transfer_ownership", params={"confirm": True}, json={"new_owner_ref": "bob"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["new_owner"]["unique_id"] == BOB
    assert client.post(f"/resources/{dataset}/transfer_ownership", json={}).status_code == 422


def test_owner_is_null_with_its_deprecated_alias(client, db_engine, auth_as):
    _people(db_engine)
    _as(auth_as, db_engine, ALICE)
    owned = _dataset(client, "owned", "bob")["unique_id"]
    for name in ("owner_is_null", "owner_id_is_null"):
        resp = client.get("/datasets", params={name: "false"})
        assert resp.status_code == 200, resp.text
        assert owned in {item["unique_id"] for item in resp.json()["items"]}
    assert client.get("/datasets", params={"owner_is_null": "true", "owner_id_is_null": "false"}).status_code == 422
    assert client.get("/samples", params={"owner_is_null": "true"}).status_code == 200
    assert client.get("/datasets/facets", params={"field": "owner", "owner_is_null": "false"}).status_code == 200
