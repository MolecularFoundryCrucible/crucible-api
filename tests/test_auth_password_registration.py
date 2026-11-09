import re

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app import config
from app.api.main import app
from app.api.routes import passwords as routes
from app.auth.throttle import attempt_password
from app.models.core_models import (
    AccessGroup, AuthEvent, DeviceAuthorization, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, LocalCredential,
    LoginThrottle, PasswordToken, User, UserAccessGroupLink,
)
from app.utils.auth_utils import authenticate_user

API_URL = "http://localhost:7777"
PASSWORD = "violet ladder under quiet rain"
PERSON = {"first_name": "Rosalind", "last_name": "Franklin", "email": "Rosalind@Example.org", "username": "rfranklin"}


@pytest.fixture
def setup(db_engine, hub_client_registered, orcid_provider_settings, monkeypatch):
    for name, value in {
        "CRUCIBLE_AUTH_PASSWORD_ENABLED": "true", "CRUCIBLE_AUTH_PASSWORD_REGISTRATION": "approval", "CRUCIBLE_AUTH_PEPPER": "p" * 40,
        "CRUCIBLE_AUTH_PEPPER_ID": "one", "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "HUB_AUTH_PUBLIC_API_URL": API_URL,
    }.items():
        monkeypatch.setenv(name, value)
    orcid_provider_settings()
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (
        LocalCredential, LoginThrottle, PasswordToken, HubTokenFamily, HubRefreshToken, HubAccessToken, HubLoginCode, HubLoginTransaction, DeviceAuthorization,
    )])
    with Session(db_engine) as session:
        session.add(User(unique_id="0tmnyhfztdwc1000w2kspzsnh0", username="ada", first_name="Ada", last_name="Lovelace", email="ada@example.org"))
        session.commit()
    app.dependency_overrides.pop(authenticate_user, None)
    return db_engine


@pytest.fixture
def client(setup):
    with TestClient(app, base_url=API_URL) as test_client:
        yield test_client


def form_token(client):
    page = client.get("/oauth/register")
    assert page.status_code == 200, page.text
    return re.search(r'name="form_token" value="([^"]+)"', page.text).group(1)


def submit(client, token=None, **changes):
    data = {**PERSON, "password": PASSWORD, "confirm": PASSWORD, "form_token": token or form_token(client), **changes}
    return client.post("/oauth/register", data=data)


def person(engine, username):
    with Session(engine) as session:
        return session.exec(select(User).where(User.username == username)).one_or_none()


def test_asking_for_an_account_creates_one_that_waits_for_approval(client, setup):
    answer = submit(client)
    assert answer.status_code == 200 and "Request sent" in answer.text, answer.text
    user = person(setup, "rfranklin")
    assert user.pending_at is not None and user.email == "rosalind@example.org" and (user.first_name, user.last_name) == ("Rosalind", "Franklin")
    assert user.platform_role == "none" and not user.is_service_account
    with Session(setup) as session:
        group = session.exec(select(AccessGroup).where(AccessGroup.group_name == user.unique_id)).one()
        assert session.exec(select(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id, UserAccessGroupLink.accessgroup_id == group.id)).one().role == "owner"
        assert session.exec(select(AuthEvent.event).where(AuthEvent.user_id == user.id)).all() == ["registered"]
        assert session.exec(select(HubTokenFamily)).all() == []
    assert attempt_password(setup, user.id, PASSWORD, None)


def test_the_sign_in_page_links_to_registration_only_when_it_is_open(client, monkeypatch):
    from app.api.routes.hub_auth import _password_page
    from starlette.requests import Request

    request = Request({"type": "http", "method": "GET", "path": "/oauth/password", "headers": [], "query_string": b""})
    assert 'href="register"' in _password_page(request, state="s").body.decode()
    monkeypatch.setenv("CRUCIBLE_AUTH_PASSWORD_REGISTRATION", "closed")
    config.get_settings.cache_clear()
    assert 'href="register"' not in _password_page(request, state="s").body.decode()


@pytest.mark.parametrize("taken", [{"username": "ada"}, {"username": "a-da"}, {"email": "ADA@example.org"}])
def test_a_username_or_email_in_use_gets_one_answer(client, setup, taken):
    answer = submit(client, **taken)
    assert answer.status_code == 409 and "This username or email is already in use" in answer.text
    assert person(setup, taken.get("username", "rfranklin")) is None or taken.get("username") == "ada"


@pytest.mark.parametrize("change,status,text", [
    ({"username": "admin"}, 422, "reserved"),
    ({"username": "x"}, 422, "3-24"),
    ({"email": "not-an-address"}, 422, "@-sign"),
    ({"first_name": " "}, 422, "first and last name"),
    ({"confirm": PASSWORD + "!"}, 422, "different"),
    ({"password": "too short", "confirm": "too short"}, 422, "at least"),
    ({"password": "rosalind@example.org", "confirm": "rosalind@example.org"}, 422, "username, email address, name"),
])
def test_each_rule_applies_and_keeps_what_was_typed(client, setup, change, status, text):
    answer = submit(client, **change)
    assert answer.status_code == status and text in answer.text, answer.text
    assert person(setup, change.get("username", "rfranklin")) is None
    if "email" not in change:
        assert 'value="Rosalind@Example.org"' in answer.text


def test_a_form_from_another_browser_is_refused(client, setup):
    token = form_token(client)
    client.cookies.clear()
    assert submit(client, token).status_code == 400
    assert person(setup, "rfranklin") is None


def test_every_submission_counts_against_the_address(client, setup):
    for index in range(11):
        assert submit(client, username=f"bot{index}a", email=f"bot{index}@example.org").status_code == 200
    slowed = submit(client, username="botlast", email="botlast@example.org")
    assert slowed.status_code == 429 and "Too many requests" in slowed.text
    assert person(setup, "botlast") is None


@pytest.mark.parametrize("setting", [{"CRUCIBLE_AUTH_PASSWORD_REGISTRATION": "closed"}, {"CRUCIBLE_AUTH_PASSWORD_ENABLED": "false"}])
def test_there_is_no_page_when_registration_or_passwords_are_off(client, monkeypatch, setting):
    for name, value in setting.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    assert client.get("/oauth/register").status_code == 404
    assert client.post("/oauth/register", data={**PERSON, "password": PASSWORD, "confirm": PASSWORD, "form_token": "x"}).status_code == 404


def test_open_registration_with_passwords_is_refused_at_startup(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_PASSWORD_REGISTRATION", "open")
    config.get_settings.cache_clear()
    with pytest.raises(config.ConfigurationError, match="refused"):
        config.check_settings()
