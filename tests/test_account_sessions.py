import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app.api.main import app
from app.cli import auth as auth_cli
from app.tables import AuthEvent, AccessToken, DeviceAuthorization, LoginCode, RefreshToken, TokenFamily, User
from app.utils import auth_utils
from app.utils.auth_utils import authenticate_user
from app.auth.tokens import AUTHORIZATION_CODE, DEVICE_CODE, issue_tokens

ADA, BOB, BOSS, CHIEF = "0tmnyhfztdwc1000w2kspzsnh0", "0tmnyhfztdwc1000w2kspzsnh1", "0tmnyhfztdwc1000w2kspzsnh9", "0tmnyhfztdwc1000w2kspzsnh8"


@pytest.fixture
def people(db_engine, hub_client_registered, orcid_provider_settings):
    orcid_provider_settings()
    SQLModel.metadata.create_all(db_engine, tables=[TokenFamily.__table__, RefreshToken.__table__, AccessToken.__table__])
    with Session(db_engine) as session:
        for unique_id, username, role in ((ADA, "ada", "none"), (BOB, "bob", "none"), (BOSS, "boss", "admin"), (CHIEF, "chief", "admin")):
            session.add(User(unique_id=unique_id, username=username, first_name="A", last_name="B", email=None, platform_role=role))
        session.commit()
    app.dependency_overrides.pop(authenticate_user, None)
    return db_engine


def user_id(engine, unique_id):
    with Session(engine) as session:
        return session.exec(select(User.id).where(User.unique_id == unique_id)).one()


def sign_in(engine, unique_id, *, age=timedelta(seconds=5), grant_type=AUTHORIZATION_CODE):
    return issue_tokens(engine, user_id(engine, unique_id), "crucible-hub", auth_time=datetime.now(timezone.utc) - age, grant_type=grant_type).access_token


def bearer(token, **extra):
    return {"Authorization": f"Bearer {token}", **extra}


def works(token):
    try:
        auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        return True
    except Exception:
        return False


@pytest.fixture
def client(people):
    with TestClient(app) as test_client:
        yield test_client


def test_people_see_their_own_sessions_with_the_current_one_marked(client, people):
    laptop, phone = sign_in(people, ADA), sign_in(people, ADA)
    sign_in(people, BOB)
    listed = client.get("/account/sessions", headers=bearer(laptop)).json()
    assert listed["total"] == 2 and [item["current"] for item in listed["items"]].count(True) == 1
    assert {item["client_name"] for item in listed["items"]} == {"Crucible Hub"}
    assert laptop not in json.dumps(listed) and phone not in json.dumps(listed)


def test_ending_one_session_needs_a_recent_sign_in_and_stops_it_at_once(client, people):
    laptop, phone = sign_in(people, ADA), sign_in(people, ADA)
    other = next(item["id"] for item in client.get("/account/sessions", headers=bearer(laptop)).json()["items"] if not item["current"])
    stale = sign_in(people, ADA, age=timedelta(hours=1))
    refused = client.delete(f"/account/sessions/{other}", headers=bearer(stale))
    assert (refused.status_code, refused.json()["detail"]["code"]) == (401, "insufficient_user_authentication")
    assert client.delete(f"/account/sessions/{other}", headers=bearer(laptop)).status_code == 204
    assert not works(phone) and works(laptop)
    assert client.delete(f"/account/sessions/{other}", headers=bearer(laptop)).status_code == 404


def test_an_api_key_can_list_but_not_end_sessions(client, people, auth_as):
    sign_in(people, ADA)
    auth_as(ADA)
    assert client.get("/account/sessions").status_code == 200
    refused = client.delete("/account/sessions")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "hub_session_required")


def test_a_device_session_works_but_cannot_end_sessions_or_create_keys(client, people):
    device = sign_in(people, ADA, grant_type=DEVICE_CODE)
    assert client.get("/account/sessions", headers=bearer(device)).status_code == 200
    for method, path, body in (("DELETE", "/account/sessions", None), ("POST", "/account/apikeys", {"name": "from a device"})):
        refused = client.request(method, path, headers=bearer(device), json=body)
        assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "browser_sign_in_required")
    assert works(device)


def test_someone_elses_session_cannot_be_ended(client, people):
    bobs = sign_in(people, BOB)
    bob_session = client.get("/account/sessions", headers=bearer(bobs)).json()["items"][0]["id"]
    assert client.delete(f"/account/sessions/{bob_session}", headers=bearer(sign_in(people, ADA))).status_code == 404
    assert works(bobs)


def test_sign_out_everywhere_else_keeps_this_session_and_is_recorded(client, people):
    here, there, elsewhere = sign_in(people, ADA), sign_in(people, ADA), sign_in(people, ADA)
    assert client.delete("/account/sessions", headers=bearer(here)).status_code == 204
    assert (works(here), works(there), works(elsewhere)) == (True, False, False)
    with Session(people) as session:
        assert session.exec(select(AuthEvent.event)).all() == ["sessions_ended"]


def test_administrators_list_and_end_a_persons_sessions(client, people):
    adas = sign_in(people, ADA)
    boss = sign_in(people, BOSS)
    listed = client.get(f"/users/{ADA}/sessions", headers=bearer(boss, **{"Crucible-Privilege-Mode": "elevated"}))
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert client.delete(f"/users/{ADA}/sessions", headers=bearer(boss, **{"Crucible-Privilege-Mode": "elevated"})).status_code == 204
    assert not works(adas) and works(boss)
    assert client.get(f"/users/{BOB}/sessions", headers=bearer(adas)).status_code == 401


def test_another_human_administrators_sessions_need_a_recent_sign_in(client, people, auth_as):
    sign_in(people, CHIEF)
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    from app.utils.permissions import PlatformRole

    auth_as(BOSS, platform_role=PlatformRole.ADMIN)
    refused = client.delete(f"/users/{CHIEF}/sessions")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "hub_session_required")


def test_ended_and_expired_sessions_are_not_listed(client, people):
    current = sign_in(people, ADA)
    sign_in(people, ADA)
    with Session(people) as session:
        family = session.exec(select(TokenFamily).order_by(TokenFamily.created_at)).all()[-1]
        family.last_refreshed_at = datetime.now(timezone.utc) - timedelta(days=8)
        session.add(family)
        session.commit()
    assert client.get("/account/sessions", headers=bearer(current)).json()["total"] == 1


def test_sessions_come_in_pages(client, people):
    tokens = [sign_in(people, ADA) for _ in range(5)]
    first = client.get("/account/sessions", headers=bearer(tokens[0]), params={"limit": 2}).json()
    seen, cursor = [item["id"] for item in first["items"]], first["next_cursor"]
    while cursor:
        page = client.get("/account/sessions", headers=bearer(tokens[0]), params={"limit": 2, "cursor": cursor}).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen)) == 5


def test_the_command_ends_one_persons_or_everyones_sessions(people, use_engine, capsys):
    use_engine(people)
    adas, bobs = sign_in(people, ADA), sign_in(people, BOB)
    args = type("Args", (), {"user": "ada", "all": False, "commit": False})()
    assert auth_cli._run_end_sessions(args) == 0 and json.loads(capsys.readouterr().out) == {"mode": "dry-run", "sessions": 1}
    assert works(adas)
    args.commit = True
    auth_cli._run_end_sessions(args)
    assert not works(adas) and works(bobs)
    auth_cli._run_end_sessions(type("Args", (), {"user": None, "all": True, "commit": True})())
    assert not works(bobs)


def pending_sign_ins(engine, unique_id, mark):
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        person = user_id(engine, unique_id)
        session.add(LoginCode(code_hash=mark * 64, user_unique_id=unique_id, user_id=person, client_id="crucible-hub", callback_uri="https://hub.example/cb",
                              code_challenge="x" * 43, created_at=now, expires_at=now + timedelta(minutes=1)))
        session.add(DeviceAuthorization(device_code_hash=mark * 64, user_code_hash=mark * 64, client_id="nano-crucible", created_at=now,
                                        expires_at=now + timedelta(minutes=5), user_id=person, approved_at=now))


def cancelled(engine, mark):
    with Session(engine) as session:
        return session.get(LoginCode, mark * 64).consumed_at is not None, session.get(DeviceAuthorization, mark * 64).denied_at is not None


def _end_through_route(client, people):
    assert client.delete("/account/sessions", headers=bearer(sign_in(people, ADA))).status_code == 204


def _end_as_administrator(client, people):
    assert client.delete(f"/users/{ADA}/sessions", headers=bearer(sign_in(people, BOSS), **{"Crucible-Privilege-Mode": "elevated"})).status_code == 204


def _disable(client, people):
    assert client.post(f"/users/{ADA}/disable", headers=bearer(sign_in(people, BOSS), **{"Crucible-Privilege-Mode": "elevated"})).status_code == 200


def _end_everyones(client, people):
    from app.auth.sessions import end_everyones_sessions
    end_everyones_sessions(people, commit=True)


@pytest.mark.parametrize("end, bob_too", [(_end_through_route, False), (_end_as_administrator, False), (_disable, False), (_end_everyones, True)])
def test_ending_all_sessions_also_cancels_sign_ins_that_were_approved_but_not_finished(client, people, end, bob_too):
    pending_sign_ins(people, ADA, "a")
    pending_sign_ins(people, BOB, "b")
    end(client, people)
    assert cancelled(people, "a") == (True, True)
    assert cancelled(people, "b") == (bob_too, bob_too)

