from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.general_utils import get_utc_datetime
from app.models.core_models import (
    AccessGroup, Dataset, DeletionRequest, Instrument, Project, Resource, ResourceACL, Sample, User, UserRecentResource,
)
from app.utils.permissions import Role

ME = "0000-0004-0000-0001"
URL = "/account/recent-resources"


def _resource(session, mfid, resource_type, readable=True, **fields):
    session.add(Resource(mfid=mfid, resource_type=resource_type))
    model = {"dataset": Dataset, "sample": Sample, "project": Project, "instrument": Instrument}[resource_type]
    session.add(model(unique_id=mfid, **fields))
    if readable:
        session.add(ResourceACL(mfid=mfid, accessgroup_id=1, read=True, write=False, permission=Role.VIEWER.db_value))


@pytest.fixture
def me(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(AccessGroup(id=1, group_name="test_group"))
        session.add(User(first_name="Re", last_name="Cent", email=None, unique_id=ME, username="recent"))
        _resource(session, "ds1", "dataset", dataset_name="Spectra", project_mfid="pr1")
        _resource(session, "sm1", "sample", sample_name="Film 7", project_mfid="pr1")
        _resource(session, "pr1", "project", project_id="proj-a", title="Project A", organization="MF", status="active", project_lead_orcid=ME, project_lead_email="lead@example.org")
        _resource(session, "in1", "instrument", instrument_id="tem-1", instrument_name="TEM", owner="facility", location="B67")
        _resource(session, "hidden", "dataset", readable=False, dataset_name="Secret")
        session.commit()
    auth_as(ME, groups=[("test_group", 1, Role.VIEWER)])
    return client


def record(client, mfid):
    return client.post(URL, json={"resource_mfid": mfid})


def rows(db_engine):
    with Session(db_engine) as session:
        return {row.resource_mfid: row.viewed_at for row in session.exec(select(UserRecentResource)).all()}


def age(db_engine, mfid, delta):
    with Session(db_engine) as session:
        row = session.exec(select(UserRecentResource).where(UserRecentResource.resource_mfid == mfid)).one()
        row.viewed_at = row.viewed_at - delta
        session.add(row)
        session.commit()


def test_record_and_list_newest_first(me, db_engine):
    for mfid in ("ds1", "sm1", "pr1", "in1"):
        assert record(me, mfid).status_code == 204
    for offset, mfid in enumerate(("in1", "pr1", "sm1", "ds1")):
        age(db_engine, mfid, timedelta(minutes=offset))
    response = me.get(URL)
    assert response.status_code == 200
    body = response.json()
    assert [item["resource_mfid"] for item in body] == ["in1", "pr1", "sm1", "ds1"]
    assert body[0] | {"viewed_at": None} == {"resource_mfid": "in1", "resource_type": "instrument", "name": "TEM", "project_id": None, "identifier": "tem-1", "viewed_at": None}
    assert {item["resource_mfid"]: (item["name"], item["project_id"], item["identifier"]) for item in body} == {
        "in1": ("TEM", None, "tem-1"), "pr1": ("Project A", "proj-a", "proj-a"), "sm1": ("Film 7", "proj-a", None), "ds1": ("Spectra", "proj-a", None),
    }
    assert [item["resource_mfid"] for item in me.get(URL, params={"limit": 2}).json()] == ["in1", "pr1"]


@pytest.mark.parametrize("limit", [0, 51])
def test_limit_bounds(me, limit):
    assert me.get(URL, params={"limit": limit}).status_code == 422


@pytest.mark.parametrize("mfid", ["missing", "hidden"])
def test_unknown_or_unreadable_is_404(me, db_engine, mfid):
    assert record(me, mfid).status_code == 404
    assert rows(db_engine) == {}


def test_repeat_view_within_a_minute_is_not_rewritten(me, db_engine):
    record(me, "ds1")
    age(db_engine, "ds1", timedelta(seconds=30))
    first = rows(db_engine)["ds1"]
    record(me, "ds1")
    assert rows(db_engine)["ds1"] == first
    age(db_engine, "ds1", timedelta(seconds=40))
    record(me, "ds1")
    assert rows(db_engine)["ds1"] > first


def test_access_loss_and_pending_deletion_are_filtered(me, db_engine):
    record(me, "ds1")
    record(me, "sm1")
    with Session(db_engine) as session:
        session.delete(session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds1")).one())
        session.add(DeletionRequest(resource_id="sm1", requester_id=ME, status="pending"))
        session.commit()
    assert me.get(URL).json() == []


def test_expired_entries_are_dropped(me, db_engine):
    record(me, "ds1")
    record(me, "sm1")
    age(db_engine, "ds1", timedelta(days=91))
    assert [item["resource_mfid"] for item in me.get(URL).json()] == ["sm1"]


def test_cap_keeps_newest_fifty(me, db_engine):
    with Session(db_engine) as session:
        user_id = session.exec(select(User.id).where(User.unique_id == ME)).one()
        now = get_utc_datetime()
        for n in range(55):
            _resource(session, f"bulk{n}", "dataset", dataset_name=f"Bulk {n}")
            session.add(UserRecentResource(user_id=user_id, resource_mfid=f"bulk{n}", viewed_at=now - timedelta(minutes=60 - n)))
        session.commit()
    record(me, "ds1")
    kept = rows(db_engine)
    assert len(kept) == 50
    assert "ds1" in kept and "bulk54" in kept and "bulk5" not in kept


def test_delete_one_and_all(me, db_engine):
    record(me, "ds1")
    record(me, "sm1")
    assert me.delete(f"{URL}/ds1").status_code == 204
    assert me.delete(f"{URL}/ds1").status_code == 204
    assert set(rows(db_engine)) == {"sm1"}
    assert me.delete(URL).status_code == 204
    assert rows(db_engine) == {}


def test_opt_out_stores_nothing(me, db_engine):
    assert me.patch("/account/preferences", json={"record_recent_resources": False}).status_code == 200
    assert record(me, "ds1").status_code == 204
    assert rows(db_engine) == {}
    assert me.patch("/account/preferences", json={"record_recent_resources": None}).status_code == 200
    record(me, "ds1")
    assert set(rows(db_engine)) == {"ds1"}


def test_history_is_caller_scoped(me, db_engine, auth_as):
    record(me, "ds1")
    with Session(db_engine) as session:
        session.add(User(first_name="Ot", last_name="Her", email=None, unique_id="0000-0004-0000-0002", username="other"))
        session.commit()
    auth_as("0000-0004-0000-0002", groups=[("test_group", 1, Role.VIEWER)])
    assert me.get(URL).json() == []
    assert me.delete(URL).status_code == 204
    assert set(rows(db_engine)) == {"ds1"}
