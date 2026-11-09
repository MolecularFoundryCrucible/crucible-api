from datetime import datetime, timedelta, timezone

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app import config
from app.api.main import app
from app.auth.api_keys import store_key
from app.auth.password_links import issue_link
from app.auth.passwords import store_password
from app.auth.throttle import attempt_password
from app.models.core_models import (
    AuthEvent, DeviceAuthorization, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, LocalCredential, LoginThrottle,
    PasswordToken, User,
)
from app.utils import auth_utils
from app.utils.auth_utils import authenticate_user
from app.utils.hub_tokens import DEVICE_CODE, issue_hub_tokens
from app.utils.permissions import PlatformRole

ADA, BOB, NEWCOMER, BOSS, CHIEF, ROBOT = (
    "0tmnyhfztdwc1000w2kspzsnh0", "0tmnyhfztdwc1000w2kspzsnh1", "0tmnyhfztdwc1000w2kspzsnh2",
    "0tmnyhfztdwc1000w2kspzsnh9", "0tmnyhfztdwc1000w2kspzsnh8", "0tmnyhfztdwc1000w2kspzsnh7",
)
OLD = "an old password nobody remembers"
NEW = "violet ladder under quiet rain"
ELEVATED = {"Crucible-Privilege-Mode": "elevated"}


@pytest.fixture
def people(db_engine, hub_client_registered, orcid_provider_settings, monkeypatch):
    for name, value in {
        "CRUCIBLE_AUTH_PASSWORD_ENABLED": "true", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one",
        "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "HUB_AUTH_PUBLIC_API_URL": "http://localhost:7777",
    }.items():
        monkeypatch.setenv(name, value)
    orcid_provider_settings()
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (
        LocalCredential, LoginThrottle, PasswordToken, HubTokenFamily, HubRefreshToken, HubAccessToken, HubLoginCode, HubLoginTransaction, DeviceAuthorization,
    )])
    with Session(db_engine) as session:
        session.add(User(unique_id=ADA, username="ada.lovelace", first_name="Ada", last_name="Lovelace", email="ada@example.org"))
        session.add(User(unique_id=BOB, username="bob", first_name="Bob", last_name="Builder"))
        session.add(User(unique_id=NEWCOMER, username=None, first_name="Grace", last_name="Hopper"))
        session.add(User(unique_id=BOSS, username="boss", first_name="Big", last_name="Boss", platform_role="admin"))
        session.add(User(unique_id=CHIEF, username="chief", first_name="Chief", last_name="Admin", platform_role="admin"))
        session.add(User(unique_id=ROBOT, username="robot", first_name="R", last_name="Bot", is_service_account=True))
        session.commit()
        store_password(session, session.exec(select(User).where(User.unique_id == ADA)).one(), OLD)
        store_password(session, session.exec(select(User).where(User.unique_id == CHIEF)).one(), OLD)
        session.commit()
    app.dependency_overrides.pop(authenticate_user, None)
    return db_engine


@pytest.fixture
def client(people):
    with TestClient(app) as test_client:
        yield test_client


def row(engine, unique_id):
    with Session(engine) as session:
        return session.exec(select(User).where(User.unique_id == unique_id)).one()


def sign_in(engine, unique_id, *, age=timedelta(seconds=5), grant_type="authorization_code"):
    return issue_hub_tokens(engine, row(engine, unique_id).id, "crucible-hub", auth_time=datetime.now(timezone.utc) - age, grant_type=grant_type).access_token


def bearer(token, *, elevated=False):
    return {"Authorization": f"Bearer {token}", **(ELEVATED if elevated else {})}


def works(token):
    try:
        auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        return True
    except Exception:
        return False


def events(engine):
    with Session(engine) as session:
        return session.exec(select(AuthEvent.event, AuthEvent.actor_user_id).order_by(AuthEvent.id)).all()


def change(client, token, **body):
    return client.post("/account/password", headers=bearer(token), json=body)


def field_error(answer):
    assert answer.status_code == 422, answer.text
    [error] = answer.json()["detail"]
    return error["loc"], error["type"]


def test_a_first_password_needs_a_username_set_first(client, people):
    token = sign_in(people, NEWCOMER)
    refused = change(client, token, new_password=NEW)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "username_required")
    assert client.patch("/account/profile", headers=bearer(token), json={"username": "grace_h"}).status_code == 200
    assert change(client, token, new_password=NEW).status_code == 204
    assert attempt_password(people, row(people, NEWCOMER).id, NEW, None)
    assert events(people)[-1] == ("password_set", row(people, NEWCOMER).id)


def test_a_username_field_in_the_body_is_not_accepted_as_a_shortcut(client, people):
    token = sign_in(people, NEWCOMER)
    assert change(client, token, new_password=NEW, username="grace_h").status_code == 409
    assert row(people, NEWCOMER).username is None


def test_changing_needs_the_current_password_and_ends_the_other_sessions(client, people):
    here, there = sign_in(people, ADA), sign_in(people, ADA)
    with Session(people) as session:
        token, _ = issue_link(session, session.get(User, row(people, ADA).id), "reset", actor=None)
        session.commit()
    assert field_error(change(client, here, new_password=NEW)) == (["body", "current_password"], "current_password_required")
    assert field_error(change(client, here, current_password="not it at all", new_password=NEW)) == (["body", "current_password"], "current_password_invalid")
    with Session(people) as session:
        assert session.get(LocalCredential, row(people, ADA).id).trusted_failed_attempts == 1
    assert change(client, here, current_password=OLD, new_password=NEW).status_code == 204
    assert works(here) and not works(there)
    assert attempt_password(people, row(people, ADA).id, NEW, None) and not attempt_password(people, row(people, ADA).id, OLD, None)
    with Session(people) as session:
        assert session.exec(select(PasswordToken)).one().voided_at is not None
    assert ("password_changed", row(people, ADA).id) in events(people)


def test_the_password_rules_apply(client, people):
    for new, reason in (("too short", "too_short"), ("ada@example.org", "personal")):
        answer = change(client, sign_in(people, ADA), current_password=OLD, new_password=new)
        assert field_error(answer) == (["body", "new_password"], "password_rejected") and answer.json()["detail"][0]["ctx"]["reason"] == reason


def test_api_keys_device_sessions_and_stale_sessions_are_refused(client, people, auth_as):
    device = change(client, sign_in(people, ADA, grant_type=DEVICE_CODE), current_password=OLD, new_password=NEW)
    assert (device.status_code, device.json()["detail"]["code"]) == (403, "browser_sign_in_required")
    stale = change(client, sign_in(people, ADA, age=timedelta(hours=1)), current_password=OLD, new_password=NEW)
    assert (stale.status_code, stale.json()["detail"]["code"]) == (401, "insufficient_user_authentication")
    auth_as(ADA)
    key = client.post("/account/password", json={"current_password": OLD, "new_password": NEW})
    assert (key.status_code, key.json()["detail"]["code"]) == (403, "hub_session_required")


def test_password_changes_never_touch_api_keys(client, people):
    with Session(people) as session:
        store_key(session, row(people, ADA).id, "crucible_" + "k" * 40)
        session.commit()
    assert change(client, sign_in(people, ADA), current_password=OLD, new_password=NEW).status_code == 204
    assert works("crucible_" + "k" * 40)


def test_no_password_changes_when_passwords_are_off(client, people, monkeypatch):
    token = sign_in(people, ADA)
    monkeypatch.setenv("CRUCIBLE_AUTH_PASSWORD_ENABLED", "false")
    config.get_settings.cache_clear()
    answer = change(client, token, current_password=OLD, new_password=NEW)
    assert (answer.status_code, answer.json()["detail"]["code"], answer.json()["detail"]["feature"]) == (501, "feature_disabled", "passwords")


def test_an_admin_key_removes_an_ordinary_persons_password(client, people, auth_as):
    adas = sign_in(people, ADA)
    auth_as(BOSS, platform_role=PlatformRole.ADMIN)
    assert client.delete(f"/users/{ADA}/password", headers=ELEVATED).status_code == 204
    assert not works(adas)
    with Session(people) as session:
        assert session.get(LocalCredential, row(people, ADA).id) is None
    assert events(people)[-1][0] == "password_removed"
    assert not attempt_password(people, row(people, ADA).id, OLD, None)
    missing = client.delete(f"/users/{ADA}/password", headers=ELEVATED)
    assert (missing.status_code, missing.json()["detail"]["code"]) == (404, "no_password")
    robot = client.delete(f"/users/{ROBOT}/password", headers=ELEVATED)
    assert robot.status_code in (404, 409)


def test_an_administrators_password_needs_a_recent_browser_session(client, people, auth_as):
    auth_as(BOSS, platform_role=PlatformRole.ADMIN)
    refused = client.delete(f"/users/{CHIEF}/password", headers=ELEVATED)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "hub_session_required")
    app.dependency_overrides.pop(authenticate_user, None)
    assert client.delete(f"/users/{CHIEF}/password", headers=bearer(sign_in(people, BOSS), elevated=True)).status_code == 204
    assert events(people)[-1] == ("password_removed", row(people, BOSS).id)


def test_ordinary_people_cannot_remove_someone_elses_password(client, people):
    answer = client.delete(f"/users/{CHIEF}/password", headers=bearer(sign_in(people, BOB)))
    assert answer.status_code in (401, 403)
    with Session(people) as session:
        assert session.get(LocalCredential, row(people, CHIEF).id) is not None


def test_removing_your_own_only_way_in_is_refused(client, people):
    answer = client.delete(f"/users/{ADA}/password", headers=bearer(sign_in(people, ADA)))
    assert (answer.status_code, answer.json()["detail"]["code"]) == (409, "last_sign_in_method")
