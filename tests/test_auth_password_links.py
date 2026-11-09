import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app import config
from app.api.main import app
from app.api.routes import passwords as routes
from app.auth.password_links import issue_link, link_token_hash
from app.auth.passwords import store_password
from app.auth.throttle import attempt_password
from app.cli import admin as admin_cli
from app.models.core_models import (
    AuthEvent, DeviceAuthorization, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, LocalCredential, LoginThrottle,
    PasswordToken, User,
)
from app.utils import auth_utils
from app.utils.auth_utils import authenticate_user
from app.utils.hub_tokens import DEVICE_CODE, issue_hub_tokens
from app.utils.permissions import PlatformRole

API_URL = "http://localhost:7777"
ADA, BOSS, CHIEF, ROBOT = "0tmnyhfztdwc1000w2kspzsnh0", "0tmnyhfztdwc1000w2kspzsnh9", "0tmnyhfztdwc1000w2kspzsnh8", "0tmnyhfztdwc1000w2kspzsnh7"
NEW = "violet ladder under quiet rain"
ELEVATED = {"Crucible-Privilege-Mode": "elevated"}


@pytest.fixture
def people(db_engine, hub_client_registered, orcid_provider_settings, monkeypatch):
    for name, value in {
        "CRUCIBLE_AUTH_PASSWORD_ENABLED": "true", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one",
        "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "HUB_AUTH_PUBLIC_API_URL": API_URL,
    }.items():
        monkeypatch.setenv(name, value)
    orcid_provider_settings()
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (
        LocalCredential, LoginThrottle, PasswordToken, HubTokenFamily, HubRefreshToken, HubAccessToken, HubLoginCode, HubLoginTransaction, DeviceAuthorization,
    )])
    with Session(db_engine) as session:
        session.add(User(unique_id=ADA, username="ada.lovelace", first_name="Ada", last_name="Lovelace", email="ada@example.org"))
        session.add(User(unique_id=BOSS, username="boss", first_name="Grace", last_name="Hopper", platform_role="admin"))
        session.add(User(unique_id=CHIEF, username="chief", first_name="Chief", last_name="Admin", platform_role="admin"))
        session.add(User(unique_id=ROBOT, username="robot", first_name="R", last_name="Bot", is_service_account=True))
        session.commit()
    app.dependency_overrides.pop(authenticate_user, None)
    return db_engine


@pytest.fixture
def client(people):
    with TestClient(app, base_url=API_URL) as test_client:
        yield test_client


def row(engine, unique_id):
    with Session(engine) as session:
        return session.exec(select(User).where(User.unique_id == unique_id)).one()


def sign_in(engine, unique_id, *, grant_type="authorization_code"):
    return issue_hub_tokens(engine, row(engine, unique_id).id, "crucible-hub", auth_time=datetime.now(timezone.utc), grant_type=grant_type).access_token


def as_admin_key(auth_as, unique_id=BOSS):
    auth_as(unique_id, platform_role=PlatformRole.ADMIN)


def issue(client, unique_id=ADA, headers=None):
    return client.post(f"/users/{unique_id}/password-link", headers={**ELEVATED, **(headers or {})})


def token_of(url):
    return parse_qs(urlparse(url).query)["token"][0]


def open_link(client, url):
    opened = client.get(url.removeprefix(API_URL), follow_redirects=False)
    assert opened.status_code == 303, opened.text
    assert opened.headers["location"] == f"{API_URL}/oauth/password-link"
    page = client.get("/oauth/password-link")
    assert page.status_code == 200, page.text
    return page, re.search(r'name="form_token" value="([^"]+)"', page.text).group(1)


def submit(client, form_token, password=NEW, confirm=None):
    return client.post("/oauth/password-link", data={"password": password, "confirm": password if confirm is None else confirm, "form_token": form_token})


def works(token):
    try:
        auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
        return True
    except Exception:
        return False


def test_an_admin_key_issues_a_link_shown_once_and_only_its_hash_is_kept(client, people, auth_as):
    as_admin_key(auth_as)
    issued = issue(client)
    assert issued.status_code == 201, issued.text
    body = issued.json()
    assert body["purpose"] == "setup" and body["url"].startswith(f"{API_URL}/oauth/password-link?token=") and "no-store" in issued.headers["cache-control"]
    assert datetime.fromisoformat(body["expires_at"]) - datetime.now(timezone.utc) > timedelta(hours=23)
    with Session(people) as session:
        stored = session.exec(select(PasswordToken)).one()
        assert stored.token_hash == link_token_hash(token_of(body["url"])) and token_of(body["url"]) not in json.dumps(stored.model_dump(mode="json"))
        assert stored.created_by_user_id == row(people, BOSS).id
        assert session.exec(select(AuthEvent.event)).all() == ["link_issued"]


def test_opening_does_not_use_the_link_and_moves_the_token_into_a_cookie(client, auth_as):
    as_admin_key(auth_as)
    url = issue(client).json()["url"]
    first = client.get(url.removeprefix(API_URL), follow_redirects=False)
    assert first.status_code == 303 and "token" not in first.headers["location"]
    assert "httponly" in first.headers["set-cookie"].lower() and "path=/oauth/password-link" in first.headers["set-cookie"].lower()
    page, _ = open_link(client, url)
    assert "Ada Lovelace" in page.text and "ada.lovelace" in page.text and "Set your password" in page.text
    csp = page.headers["content-security-policy"]
    assert "form-action 'self'" in csp and "script-src 'nonce-" in csp and "unsafe-inline" not in csp and page.headers["cache-control"] == "no-store"


def test_setting_a_password_uses_the_link_once_and_signs_nobody_in(client, people, auth_as):
    as_admin_key(auth_as)
    url = issue(client).json()["url"]
    _, form_token = open_link(client, url)
    done = submit(client, form_token)
    assert done.status_code == 200 and "Password set" in done.text
    assert "set-cookie" in done.headers and "crucible_password_link=" in done.headers["set-cookie"]
    with Session(people) as session:
        assert session.exec(select(HubTokenFamily)).all() == []
        assert session.exec(select(AuthEvent.event).order_by(AuthEvent.id)).all() == ["link_issued", "password_set"]
    assert attempt_password(people, row(people, ADA).id, NEW, None)
    assert client.get(url.removeprefix(API_URL), follow_redirects=False).status_code == 400
    again = submit(client, form_token)
    assert again.status_code == 400


def test_a_reset_clears_the_lock_and_ends_sessions_codes_and_device_approvals(client, people, auth_as):
    ada = row(people, ADA)
    with Session(people) as session:
        credential = store_password(session, session.get(User, ada.id), "an old password nobody remembers")
        credential.locked_at = datetime.now(timezone.utc)
        credential.failed_attempts = 50
        session.add(HubLoginCode(code_hash="c" * 64, user_unique_id=ADA, user_id=ada.id, client_id="crucible-hub", callback_uri="https://hub.example/cb",
                                 code_challenge="x" * 43, created_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(minutes=1)))
        session.add(DeviceAuthorization(device_code_hash="d" * 64, user_code_hash="u" * 64, client_id="nano-crucible", created_at=datetime.now(timezone.utc),
                                        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5), user_id=ada.id, approved_at=datetime.now(timezone.utc)))
        session.commit()
    session_token = sign_in(people, ADA)
    as_admin_key(auth_as)
    issued = issue(client).json()
    assert issued["purpose"] == "reset"
    page, form_token = open_link(client, issued["url"])
    assert "Choose a new password" in page.text
    assert "signed out everywhere" in submit(client, form_token).text
    assert not works(session_token)
    with Session(people) as session:
        credential = session.get(LocalCredential, ada.id)
        assert (credential.locked_at, credential.failed_attempts, credential.blocked_until) == (None, 0, None)
        assert session.get(HubLoginCode, "c" * 64).consumed_at is not None
        assert session.get(DeviceAuthorization, "d" * 64).denied_at is not None
        assert session.exec(select(AuthEvent.event).order_by(AuthEvent.id.desc())).first() == "password_reset"
    assert attempt_password(people, ada.id, NEW, None)


def test_a_new_link_replaces_the_old_one(client, auth_as):
    as_admin_key(auth_as)
    old = issue(client).json()["url"]
    new = issue(client).json()["url"]
    assert client.get(old.removeprefix(API_URL), follow_redirects=False).status_code == 400
    assert client.get(new.removeprefix(API_URL), follow_redirects=False).status_code == 303


def test_the_database_allows_one_live_link_per_person(people):
    now = datetime.now(timezone.utc)
    with Session(people) as session:
        for digest in ("a" * 64, "b" * 64):
            session.add(PasswordToken(token_hash=digest, user_id=row(people, ADA).id, purpose="setup", created_at=now, expires_at=now + timedelta(hours=1)))
        with pytest.raises(IntegrityError):
            session.commit()


def test_an_expired_link_is_refused(client, people, auth_as):
    as_admin_key(auth_as)
    url = issue(client).json()["url"]
    with Session(people) as session:
        session.execute(update(PasswordToken).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        session.commit()
    refused = client.get(url.removeprefix(API_URL), follow_redirects=False)
    assert refused.status_code == 400 and "not valid" in refused.text


@pytest.mark.parametrize("cancel", ["disable", "password", "unlink"])
def test_disabling_a_password_change_or_a_login_change_cancels_the_link(client, people, auth_as, cancel):
    from app.auth.password_links import void_links

    as_admin_key(auth_as)
    url = issue(client).json()["url"]
    if cancel == "disable":
        auth_as(BOSS, platform_role=PlatformRole.ADMIN)
        assert client.post(f"/users/{ADA}/disable", headers=ELEVATED).status_code == 200
    else:
        with Session(people) as session:
            ada = session.get(User, row(people, ADA).id)
            if cancel == "password":
                store_password(session, ada, "a different password altogether")
            else:
                void_links(session, ada)
            session.commit()
    assert client.get(url.removeprefix(API_URL), follow_redirects=False).status_code == 400


def test_a_form_from_another_browser_is_refused(client, people, auth_as):
    as_admin_key(auth_as)
    _, form_token = open_link(client, issue(client).json()["url"])
    client.cookies.clear()
    assert submit(client, form_token).status_code == 400
    with Session(people) as session:
        assert session.get(LocalCredential, row(people, ADA).id) is None


def test_different_or_weak_passwords_keep_the_link(client, people, auth_as):
    as_admin_key(auth_as)
    _, form_token = open_link(client, issue(client).json()["url"])
    differ = submit(client, form_token, confirm=NEW + "!")
    assert differ.status_code == 422 and "different" in differ.text
    weak = submit(client, form_token, password="ada.lovelace@example.org"[:11])
    assert weak.status_code == 422
    personal = submit(client, form_token, password="ada@example.org")
    assert personal.status_code == 422
    assert submit(client, form_token).status_code == 200


def test_opening_and_submitting_count_against_the_address(client, auth_as):
    as_admin_key(auth_as)
    url = issue(client).json()["url"]
    for _ in range(11):
        assert client.get("/oauth/password-link?token=" + "x" * 43, follow_redirects=False).status_code == 400
    assert client.get(url.removeprefix(API_URL), follow_redirects=False).status_code == 400


def test_the_token_never_reaches_the_logs(client, auth_as, caplog):
    caplog.set_level(logging.DEBUG)
    as_admin_key(auth_as)
    url = issue(client).json()["url"]
    _, form_token = open_link(client, url)
    submit(client, form_token)
    server = "\n".join(record.getMessage() for record in caplog.records if record.name != "httpx")
    assert "password-link" in server and token_of(url) not in server and NEW not in server


def test_who_may_issue_a_link(client, people, auth_as):
    as_admin_key(auth_as)
    for target, code in ((CHIEF, "hub_session_required"), (BOSS, "hub_session_required")):
        refused = issue(client, target)
        assert (refused.status_code, refused.json()["detail"]["code"]) == (403, code)
    refused = issue(client, ROBOT)
    assert refused.status_code in (404, 409)
    auth_as(ADA)
    assert issue(client, BOSS).status_code in (401, 403)
    app.dependency_overrides.pop(authenticate_user, None)
    boss = sign_in(people, BOSS)
    assert issue(client, CHIEF, {"Authorization": f"Bearer {boss}"}).status_code == 201
    device = sign_in(people, BOSS, grant_type=DEVICE_CODE)
    refused = issue(client, ADA, {"Authorization": f"Bearer {device}"})
    assert (refused.status_code, refused.json()["detail"]["code"]) == (403, "browser_sign_in_required")


def test_service_accounts_never_get_a_link(people):
    from app.auth.password_links import LinkRefused

    with Session(people) as session:
        with pytest.raises(LinkRefused) as refused:
            issue_link(session, session.get(User, row(people, ROBOT).id), "setup", actor=None)
    assert refused.value.code == "service_account"


def test_no_link_when_passwords_are_off(client, auth_as, monkeypatch):
    as_admin_key(auth_as)
    monkeypatch.setenv("CRUCIBLE_AUTH_PASSWORD_ENABLED", "false")
    config.get_settings.cache_clear()
    refused = issue(client)
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "passwords_off")
    assert client.get("/oauth/password-link").status_code == 404


def test_the_command_prints_a_link_that_lets_a_locked_out_admin_back_in(client, people, capsys):
    with Session(people) as session:
        store_password(session, session.get(User, row(people, BOSS).id), "the forgotten administrator password")
        session.exec(select(LocalCredential)).one().locked_at = datetime.now(timezone.utc)
        session.commit()
    assert admin_cli._run_password_link(type("Args", (), {"user": "boss"})()) == 0
    url = capsys.readouterr().out.strip()
    _, form_token = open_link(client, url)
    assert submit(client, form_token).status_code == 200
    assert attempt_password(people, row(people, BOSS).id, NEW, None)
    assert admin_cli._run_password_link(type("Args", (), {"user": "robot"})()) == 1


def test_cleanup_removes_links_a_day_after_they_expire(client, people, auth_as):
    from app.utils.hub_auth_maintenance import cleanup_hub_auth

    as_admin_key(auth_as)
    issue(client)
    assert cleanup_hub_auth(people, commit=True, now=datetime.now(timezone.utc) + timedelta(hours=30))["password_token"] == 0
    assert cleanup_hub_auth(people, commit=True, now=datetime.now(timezone.utc) + timedelta(hours=49))["password_token"] == 1


@pytest.mark.parametrize("hours", ["0", "-1", "169"])
def test_link_lifetimes_out_of_range_are_refused(monkeypatch, hours):
    monkeypatch.setenv("CRUCIBLE_AUTH_PASSWORD_LINK_HOURS", hours)
    config.get_settings.cache_clear()
    with pytest.raises((ValidationError, config.ConfigurationError)):
        config.get_settings()
