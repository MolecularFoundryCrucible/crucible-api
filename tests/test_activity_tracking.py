from datetime import timedelta

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session, select

import app.services.activity as activity
import app.utils.auth_utils as auth_utils
from app.general_utils import get_utc_datetime
from app.auth.api_keys import key_hash, store_key
from app.tables import ApiKey, ApiToken, Dataset, Instrument, Project, Resource, Sample, User
from app.utils.permissions import PlatformRole
from tests.people import internal_id

ADMIN = "0000-0007-0000-0001"
ALICE = "0000-0007-0000-0002"
BOB = "0000-0007-0000-0003"
KEY = "k" * 64


@pytest.fixture(autouse=True)
def fresh_memo():
    activity._recently_recorded.clear()
    yield
    activity._recently_recorded.clear()


@pytest.fixture
def people(db_engine):
    with Session(db_engine) as session:
        for orcid, name in ((ADMIN, "admin"), (ALICE, "alice"), (BOB, "bob")):
            session.add(User(first_name=name, last_name=name, email=None, unique_id=orcid, username=name))
        session.add(ApiToken(token=KEY, user_id=internal_id(session, ALICE), user_unique_id=ALICE, access_group_id=ALICE, token_created=get_utc_datetime().date().isoformat()))
        store_key(session, internal_id(session, ALICE), KEY)
        session.commit()
    return db_engine


def user_field(engine, orcid, field="last_active_at"):
    with Session(engine) as session:
        return getattr(session.exec(select(User).where(User.unique_id == orcid)).one(), field)


def key_last_used(engine):
    with Session(engine) as session:
        return session.exec(select(ApiKey).where(ApiKey.key_hash == key_hash(KEY))).one().last_used_at


def shift(engine, model, where, field, delta):
    with Session(engine) as session:
        row = session.exec(select(model).where(where)).one()
        setattr(row, field, getattr(row, field) - delta)
        session.add(row)
        session.commit()


def test_caller_activity_is_throttled(people):
    activity.record_caller_activity(people, ALICE, KEY)
    first_user, first_key = user_field(people, ALICE), key_last_used(people)
    assert first_user is not None and first_key is not None

    activity._recently_recorded.clear()
    activity.record_caller_activity(people, ALICE, KEY)
    assert user_field(people, ALICE) == first_user
    assert key_last_used(people) == first_key

    shift(people, ApiKey, ApiKey.key_hash == key_hash(KEY), "last_used_at", timedelta(minutes=11))
    shift(people, User, User.unique_id == ALICE, "last_active_at", timedelta(minutes=30))
    activity._recently_recorded.clear()
    activity.record_caller_activity(people, ALICE, KEY)
    assert key_last_used(people) > first_key - timedelta(minutes=11)
    assert user_field(people, ALICE) == first_user - timedelta(minutes=30)

    shift(people, User, User.unique_id == ALICE, "last_active_at", timedelta(minutes=31))
    activity._recently_recorded.clear()
    activity.record_caller_activity(people, ALICE)
    assert user_field(people, ALICE) > first_user - timedelta(minutes=61)


def test_a_database_error_never_puts_the_key_in_the_log(people, monkeypatch, caplog):
    def broken(*args, **kwargs):
        raise RuntimeError(f"cannot run with {KEY}")

    monkeypatch.setattr(Session, "exec", broken)
    activity.record_caller_activity(people, ALICE, KEY)
    assert "Could not record activity" in caplog.text and KEY not in caplog.text


def test_the_server_engine_hides_statement_parameters(monkeypatch):
    from app.utils import sql_utils

    seen = {}
    monkeypatch.setattr(sql_utils, "get_secret", lambda name: "pw")
    monkeypatch.setattr(sql_utils, "create_engine", lambda url, **kwargs: seen.update(kwargs))
    sql_utils.setup_engine()
    assert seen.get("hide_parameters") is True


def test_api_key_authentication_records_activity(use_engine, people, monkeypatch):
    use_engine(people)
    monkeypatch.setattr(auth_utils, "_PUBLIC_GROUP_ID", None)
    auth_utils._auth_cache.clear()
    auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=KEY))
    assert user_field(people, ALICE) is not None
    assert key_last_used(people) is not None
    assert user_field(people, BOB) is None


@pytest.fixture
def records(people):
    with Session(people) as session:
        session.add(Project(project_id="proj-a", unique_id="pa", organization="MF", project_lead_orcid=ALICE))
        session.add(Project(project_id="proj-b", unique_id="pb", organization="MF", project_lead_orcid=ALICE))
        session.add(Instrument(unique_id="in1", instrument_name="TEM", owner=ALICE, location="B67"))
        for mfid, kind in (("pa", "project"), ("pb", "project"), ("in1", "instrument")):
            session.add(Resource(mfid=mfid, resource_type=kind))
        session.commit()
    return people


def project_activity(engine, mfid):
    with Session(engine) as session:
        return session.exec(select(Project.last_activity_at).where(Project.unique_id == mfid)).one()


def instrument_activity(engine):
    with Session(engine) as session:
        return session.exec(select(Instrument.last_activity_at).where(Instrument.unique_id == "in1")).one()


def test_creating_records_marks_project_and_instrument_active(records):
    assert project_activity(records, "pa") is None
    with Session(records) as session:
        session.add(Resource(mfid="ds1", resource_type="dataset"))
        session.add(Dataset(unique_id="ds1", dataset_name="d", project_id="proj-a", project_mfid="pa", instrument_mfid="in1"))
        session.add(Resource(mfid="sm1", resource_type="sample"))
        session.add(Sample(unique_id="sm1", sample_name="s", project_id="proj-b", project_mfid="pb"))
        session.commit()
    assert project_activity(records, "pa") is not None
    assert project_activity(records, "pb") is not None
    assert instrument_activity(records) is not None


def test_project_activity_is_throttled_and_follows_resource_changes(records):
    with Session(records) as session:
        session.add(Resource(mfid="ds1", resource_type="dataset"))
        session.add(Dataset(unique_id="ds1", dataset_name="d", project_mfid="pa", instrument_mfid="in1"))
        session.commit()
    first = project_activity(records, "pa")

    shift(records, Project, Project.unique_id == "pa", "last_activity_at", timedelta(minutes=30))
    with Session(records) as session:
        resource = session.get(Resource, "ds1")
        resource.modification_time = get_utc_datetime()
        session.add(resource)
        session.commit()
    assert project_activity(records, "pa") == first - timedelta(minutes=30)

    shift(records, Project, Project.unique_id == "pa", "last_activity_at", timedelta(minutes=31))
    with Session(records) as session:
        resource = session.get(Resource, "ds1")
        resource.modification_time = get_utc_datetime()
        session.add(resource)
        session.commit()
    assert project_activity(records, "pa") > first - timedelta(minutes=1)


def test_reads_do_not_count_as_activity(records):
    with Session(records) as session:
        session.add(Resource(mfid="ds1", resource_type="dataset"))
        session.add(Dataset(unique_id="ds1", dataset_name="d", project_mfid="pa"))
        session.commit()
    with Session(records) as session:
        project = session.exec(select(Project).where(Project.unique_id == "pa")).one()
        project.last_activity_at = None
        session.add(project)
        session.commit()
    with Session(records) as session:
        session.get(Dataset, 1)
        session.exec(select(Resource)).all()
        session.commit()
    assert project_activity(records, "pa") is None


def test_admin_filters_and_sorts_users_by_activity(client, people, auth_as):
    now = get_utc_datetime()
    with Session(people) as session:
        for orcid, ago in ((ALICE, timedelta(minutes=5)), (ADMIN, timedelta(days=3))):
            user = session.exec(select(User).where(User.unique_id == orcid)).one()
            user.last_active_at = now - ago
            session.add(user)
        session.commit()
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    recent = client.get("/users", params={"active_since": (now - timedelta(hours=1)).isoformat()}).json()
    assert [user["unique_id"] for user in recent["items"]] == [ALICE]
    assert recent["items"][0]["last_active_at"] is not None

    stale = client.get("/users", params={"inactive_since": (now - timedelta(days=1)).isoformat()}).json()
    assert {user["unique_id"] for user in stale["items"]} == {ADMIN, BOB}
    assert [user["unique_id"] for user in client.get("/users", params={"never_active": True}).json()["items"]] == [BOB]

    ordered = client.get("/users", params={"sort": "last_active", "direction": "desc", "limit": 1}).json()
    assert ordered["items"][0]["unique_id"] == ALICE
    following = client.get("/users", params={"sort": "last_active", "direction": "desc", "limit": 1, "cursor": ordered["next_cursor"]}).json()
    assert following["items"][0]["unique_id"] == ADMIN


@pytest.mark.parametrize("params", [{"sort": "last_active"}, {"active_since": "2026-01-01T00:00:00Z"}, {"inactive_since": "2026-01-01T00:00:00Z"}, {"never_active": "true"}])
def test_activity_filters_are_admin_only(client, people, auth_as, params):
    auth_as(BOB)
    assert client.get("/users", params=params).status_code == 403


def test_last_active_is_hidden_from_other_users(client, people, auth_as):
    with Session(people) as session:
        user = session.exec(select(User).where(User.unique_id == ALICE)).one()
        user.last_active_at = get_utc_datetime()
        session.add(user)
        session.commit()
    auth_as(BOB, groups=[("shared", 7)])
    response = client.get(f"/users/{ALICE}")
    assert response.status_code == 200, response.text
    assert "last_active_at" not in response.json()
    auth_as(ALICE)
    assert client.get("/account/profile").json()["last_active_at"] is not None


def test_projects_and_instruments_sort_and_filter_by_activity(client, records, auth_as):
    now = get_utc_datetime()
    with Session(records) as session:
        for mfid, ago in (("pa", timedelta(days=2)), ("pb", timedelta(minutes=10))):
            project = session.exec(select(Project).where(Project.unique_id == mfid)).one()
            project.last_activity_at = now - ago
            session.add(project)
        instrument = session.exec(select(Instrument)).one()
        instrument.last_activity_at = now
        session.add(instrument)
        session.commit()
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)

    projects = client.get("/projects", params={"sort": "activity"}).json()["items"]
    assert [project["unique_id"] for project in projects] == ["pb", "pa"]
    assert projects[0]["last_activity_at"] is not None
    recent = client.get("/projects", params={"active_since": (now - timedelta(days=1)).isoformat()}).json()["items"]
    assert [project["unique_id"] for project in recent] == ["pb"]

    instruments = client.get("/instruments", params={"sort": "activity", "active_since": (now - timedelta(hours=1)).isoformat()}).json()["items"]
    assert [instrument["unique_id"] for instrument in instruments] == ["in1"]
    assert instruments[0]["last_activity_at"] is not None


def test_key_verify_reports_last_use(client, people, auth_as):
    activity.record_caller_activity(people, ALICE, KEY)
    auth_as(ALICE)
    assert client.get("/account/verify", headers={"Authorization": f"Bearer {KEY}"}).json()["last_used_at"] is not None
