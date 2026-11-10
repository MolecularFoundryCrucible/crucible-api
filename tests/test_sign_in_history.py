from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import config
from app.api.main import app
from app.auth import throttle
from app.auth.events import AuthEventType, record_event
from app.auth.passwords import store_password
from app.auth.throttle import attempt_password
from app.tables import AuthEvent, User
from app.utils.permissions import PlatformRole

ADA, BOSS = "0tmnyhfztdwc1000w2kspzsnh0", "0tmnyhfztdwc1000w2kspzsnh9"
RIGHT = "violet ladder under quiet rain"
API_URL = "http://localhost:7777"


@pytest.fixture
def people(db_engine, monkeypatch):
    for name, value in {"CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one", "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456",
                        "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "HUB_AUTH_PUBLIC_API_URL": API_URL}.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    with Session(db_engine) as session:
        ada = User(unique_id=ADA, username="ada", first_name="Ada", last_name="L")
        session.add_all([ada, User(unique_id=BOSS, username="boss", first_name="B", last_name="B", platform_role="admin")])
        session.commit()
        store_password(session, ada, RIGHT)
        session.commit()
        return db_engine, ada.id


def history(engine):
    with Session(engine) as session:
        return session.exec(select(AuthEvent.event, AuthEvent.issuer).order_by(AuthEvent.id)).all()


def test_a_wrong_password_is_recorded_and_a_right_one_is_not(people):
    engine, ada = people
    assert not attempt_password(engine, ada, "not the right password", None)
    assert attempt_password(engine, ada, RIGHT, None)
    assert history(engine) == [("sign_in_failed", API_URL)]


def test_the_lock_is_recorded_once_and_refused_attempts_are_not(people):
    engine, ada = people
    for _ in range(throttle.KNOWN_LIMIT):
        attempt_password(engine, ada, "not the right password", None, session_authenticated=True)
    for _ in range(5):
        attempt_password(engine, ada, "not the right password", None, session_authenticated=True)
    events = [event for event, _ in history(engine)]
    assert events.count("sign_in_failed") == throttle.KNOWN_LIMIT and events.count("locked") == 1


def test_an_unknown_account_leaves_no_history(people):
    engine, _ = people
    assert not attempt_password(engine, None, "anything at all here", None)
    assert history(engine) == []


@pytest.fixture
def client(people):
    with TestClient(app) as test_client:
        yield test_client


def test_administrators_read_a_persons_history_newest_first_in_pages(client, people, auth_as):
    engine, ada = people
    with Session(engine) as session:
        person, boss = session.get(User, ada), session.exec(select(User).where(User.unique_id == BOSS)).one()
        for event, actor in ((AuthEventType.SIGN_IN, None), (AuthEventType.LINK_ISSUED, boss), (AuthEventType.PASSWORD_RESET, person), (AuthEventType.SIGN_IN, None)):
            record_event(session, event, user=person, actor=actor)
        session.commit()
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(BOSS, platform_role=PlatformRole.ADMIN)
    first = client.get(f"/users/{ADA}/auth-events", params={"limit": 3}).json()
    assert first["total"] == 4 and [item["event"] for item in first["items"]] == ["sign_in", "password_reset", "link_issued"]
    assert [item["actor_unique_id"] for item in first["items"]] == [None, None, BOSS]
    rest = client.get(f"/users/{ADA}/auth-events", params={"limit": 3, "cursor": first["next_cursor"]}).json()
    assert [item["event"] for item in rest["items"]] == ["sign_in"] and rest["next_cursor"] is None
    assert "client_address" not in first["items"][0]
    auth_as(ADA)
    assert client.get(f"/users/{ADA}/auth-events").status_code in (401, 403)


def test_a_waiting_account_has_a_history_for_administrators(client, people, auth_as):
    engine, _ = people
    with Session(engine) as session:
        waiting = User(unique_id="0tmnyhfztdwc1000w2kspzsnh5", username="waiting", first_name="W", last_name="A", pending_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        session.add(waiting)
        session.commit()
        record_event(session, AuthEventType.REGISTERED, user=waiting)
        session.commit()
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(BOSS, platform_role=PlatformRole.ADMIN)
    assert [item["event"] for item in client.get("/users/waiting/auth-events").json()["items"]] == ["registered"]


def test_people_read_their_own_history_and_nobody_elses(client, people, auth_as):
    engine, ada = people
    with Session(engine) as session:
        person, boss = session.get(User, ada), session.exec(select(User).where(User.unique_id == BOSS)).one()
        for event, about, actor in ((AuthEventType.SIGN_IN, person, None), (AuthEventType.SIGN_IN, boss, None), (AuthEventType.LINK_ISSUED, person, boss), (AuthEventType.PASSWORD_RESET, person, person)):
            record_event(session, event, user=about, actor=actor)
        session.commit()
    auth_as(ADA)
    first = client.get("/account/auth-events", params={"limit": 2}).json()
    assert first["total"] == 3 and [(item["event"], item["actor_unique_id"]) for item in first["items"]] == [("password_reset", None), ("link_issued", BOSS)]
    assert "client_address" not in first["items"][0]
    rest = client.get("/account/auth-events", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [item["event"] for item in rest["items"]] == ["sign_in"] and rest["next_cursor"] is None
    auth_as(BOSS, platform_role=PlatformRole.ADMIN)
    assert [item["event"] for item in client.get("/account/auth-events").json()["items"]] == ["sign_in"]
    assert client.get("/account/auth-events", params={"cursor": first["next_cursor"]}).status_code == 422
