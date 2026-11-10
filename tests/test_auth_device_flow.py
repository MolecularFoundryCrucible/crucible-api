import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlmodel import Session, SQLModel, select

from app import config
from app.api import main
from app.api.dependencies import get_engine
from app.api.routes import device as device_routes
from app.api.routes import hub_auth as routes
from app.api.routes import oauth as oauth_routes
from app.auth.devices import USER_CODE_ALPHABET, normalized_user_code
from app.auth.passwords import store_password
from app.tables import (
    DeviceAuthorization, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, LocalCredential, LoginThrottle, User,
)

API_URL = "http://localhost:7777"
RIGHT = "violet ladder under quiet rain"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLI = {"key": "nanocrucible", "client_id": "nano-crucible", "display_name": "nano-crucible", "type": "public", "redirect_uris": ["http://127.0.0.1/callback"]}
HUB = {"key": "hub", "client_id": "crucible-hub", "display_name": "Crucible Hub", "type": "confidential", "redirect_uris": ["https://hub.example/hub/auth/callback"]}


@pytest.fixture
def client(db_engine, monkeypatch):
    for name, value in {
        "CRUCIBLE_AUTH_CLIENTS": json.dumps([HUB, CLI]), "OAUTH_HUB_CLIENT_SECRET": "h" * 40, "HUB_AUTH_PUBLIC_API_URL": API_URL,
        "CRUCIBLE_AUTH_PASSWORD_ENABLED": "true", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one",
        "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2",
    }.items():
        monkeypatch.setenv(name, value)
    for name in ("HUB_AUTH_ENABLED", "CRUCIBLE_AUTH_PROVIDERS"):
        monkeypatch.delenv(name, raising=False)
    for module in (routes, device_routes):
        monkeypatch.setattr(module, "get_api_root_path", lambda: "")
    config.get_settings.cache_clear()
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginTransaction, HubLoginCode, HubTokenFamily, HubRefreshToken, HubAccessToken, LocalCredential, LoginThrottle, DeviceAuthorization)])
    with Session(db_engine) as session:
        user = User(unique_id="0tmnyhfztdwc1000w2kspzsnh0", username="ada.lovelace", first_name="Ada", last_name="Lovelace")
        session.add(user)
        session.commit()
        store_password(session, user, RIGHT)
        session.commit()
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, main.handle_request_validation)
    for router in (routes.router, routes.password_router, oauth_routes.router, oauth_routes.discovery, device_routes.router):
        app.include_router(router)
    app.dependency_overrides[get_engine] = lambda: db_engine
    with TestClient(app, base_url=API_URL) as test_client:
        yield test_client


def start_device(client, client_id="nano-crucible"):
    return client.post("/oauth/device_authorization", data={"client_id": client_id})


def poll(client, device_code):
    return client.post("/oauth/token", data={"grant_type": GRANT, "device_code": device_code, "client_id": "nano-crucible"})


def allow_polling(engine):
    with Session(engine) as session:
        session.execute(update(DeviceAuthorization).values(last_polled_at=None, interval=5))
        session.commit()


def approve_in_browser(client, user_code, decision="approve"):
    entered = client.post("/oauth/device", data={"user_code": user_code}, follow_redirects=False)
    assert entered.status_code == 303, entered.text
    authorize = client.get(entered.headers["location"].removeprefix(API_URL), follow_redirects=False)
    assert authorize.status_code == 302, authorize.text
    form = urlparse(authorize.headers["location"])
    state = parse_qs(form.query)["state"][0]
    signed_in = client.post("/oauth/password", data={"username": "ada.lovelace", "password": RIGHT, "state": state}, follow_redirects=False)
    assert signed_in.status_code == 303, signed_in.text
    confirm = client.get(signed_in.headers["location"].removeprefix(API_URL))
    assert confirm.status_code == 200, confirm.text
    token = re.search(r'name="decision_token" value="([^"]+)"', confirm.text).group(1)
    return confirm, client.post("/oauth/device/approve", data={"decision_token": token, "decision": decision})


def test_a_device_is_approved_in_a_browser_and_gets_its_session_once(client, db_engine):
    started = start_device(client)
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["verification_uri"] == f"{API_URL}/oauth/device" and body["verification_uri_complete"].startswith(body["verification_uri"] + "?user_code=")
    assert (body["expires_in"], body["interval"]) == (600, 5)
    assert re.fullmatch(f"[{USER_CODE_ALPHABET}]{{4}}-[{USER_CODE_ALPHABET}]{{4}}", body["user_code"])
    assert poll(client, body["device_code"]).json()["error"] == "authorization_pending"
    assert poll(client, body["device_code"]).json()["error"] == "slow_down"
    page = client.get("/oauth/device", params={"user_code": body["user_code"]})
    assert page.status_code == 200 and body["user_code"] in page.text and "Never enter a code someone sent you" in page.text
    confirm, decided = approve_in_browser(client, " " + body["user_code"].lower().replace("-", " ") + " ")
    assert "nano-crucible" in confirm.text and "Ada Lovelace" in confirm.text and body["user_code"] in confirm.text and "testclient" in confirm.text
    assert decided.status_code == 200 and "approved" in decided.text.lower()
    allow_polling(db_engine)
    tokens = poll(client, body["device_code"])
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["refresh_token"].startswith("chbr_") and abs(tokens.json()["auth_time"] - datetime.now(timezone.utc).timestamp()) < 30
    with Session(db_engine) as session:
        assert session.exec(select(HubTokenFamily.grant_type)).one() == "urn:ietf:params:oauth:grant-type:device_code"
    allow_polling(db_engine)
    assert poll(client, body["device_code"]).status_code == 400


def test_denying_ends_the_device_sign_in(client, db_engine):
    body = start_device(client).json()
    _, decided = approve_in_browser(client, body["user_code"], decision="deny")
    assert "not approved" in decided.text
    allow_polling(db_engine)
    assert poll(client, body["device_code"]).json()["error"] == "access_denied"


def test_an_expired_device_code_says_so(client, db_engine):
    body = start_device(client).json()
    with Session(db_engine) as session:
        session.execute(update(DeviceAuthorization).values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        session.commit()
    assert poll(client, body["device_code"]).json()["error"] == "expired_token"
    refused = client.post("/oauth/device", data={"user_code": body["user_code"]}, follow_redirects=False)
    assert refused.status_code == 400 and "not valid or has expired" in refused.text


def test_only_public_clients_get_device_codes_and_the_approval_client_gets_no_tokens(client):
    assert start_device(client, "crucible-hub").json()["error"] in ("invalid_client", "unauthorized_client")
    assert start_device(client, "crucible-device-approval").json()["error"] == "invalid_client"
    assert start_device(client, "nobody").json()["error"] == "invalid_client"


def test_wrong_codes_count_against_the_address_and_are_answered_alike(client, db_engine):
    body = start_device(client).json()
    for _ in range(10):
        wrong = client.post("/oauth/device", data={"user_code": "BBBB-BBBB"}, follow_redirects=False)
        assert wrong.status_code == 400 and "not valid or has expired" in wrong.text
    client.post("/oauth/device", data={"user_code": "BBBB-BBBB"}, follow_redirects=False)
    blocked = client.post("/oauth/device", data={"user_code": body["user_code"]}, follow_redirects=False)
    assert blocked.status_code == 400 and "not valid or has expired" in blocked.text


def test_codes_are_stored_only_as_hashes(client, db_engine):
    body = start_device(client).json()
    with Session(db_engine) as session:
        row = session.exec(select(DeviceAuthorization)).one()
    stored = json.dumps(row.model_dump(mode="json"))
    assert body["device_code"] not in stored and normalized_user_code(body["user_code"]) not in stored and body["user_code"] not in stored


def test_a_decision_from_another_browser_is_refused(client, db_engine):
    body = start_device(client).json()
    entered = client.post("/oauth/device", data={"user_code": body["user_code"]}, follow_redirects=False)
    authorize = client.get(entered.headers["location"].removeprefix(API_URL), follow_redirects=False)
    state = parse_qs(urlparse(authorize.headers["location"]).query)["state"][0]
    signed_in = client.post("/oauth/password", data={"username": "ada.lovelace", "password": RIGHT, "state": state}, follow_redirects=False)
    confirm = client.get(signed_in.headers["location"].removeprefix(API_URL))
    token = re.search(r'name="decision_token" value="([^"]+)"', confirm.text).group(1)
    client.cookies.clear()
    refused = client.post("/oauth/device/approve", data={"decision_token": token, "decision": "approve"})
    assert refused.status_code == 400
    allow_polling(db_engine)
    assert poll(client, body["device_code"]).json()["error"] == "authorization_pending"


def test_a_callback_not_started_in_this_browser_is_refused(client):
    assert client.get("/oauth/device/callback", params={"state": "x", "code": "y" * 40, "iss": API_URL}).status_code == 400


def test_pages_have_a_strict_csp(client):
    page = client.get("/oauth/device")
    csp = page.headers["content-security-policy"]
    assert "default-src 'none'" in csp and "form-action 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert page.headers["cache-control"] == "no-store"


def test_metadata_names_the_device_endpoint(client):
    metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["device_authorization_endpoint"] == f"{API_URL}/oauth/device_authorization"
    assert GRANT in metadata["grant_types_supported"]


def test_cleanup_removes_device_rows_a_day_after_they_expire(client, db_engine):
    from tests.conftest import TEST_TABLES
    from app.tables import AuthEvent
    from app.utils.hub_auth_maintenance import cleanup_hub_auth

    SQLModel.metadata.create_all(db_engine, tables=list(dict.fromkeys(TEST_TABLES + [AuthEvent.__table__])))
    start_device(client)
    assert cleanup_hub_auth(db_engine, commit=True, now=datetime.now(timezone.utc) + timedelta(hours=2))["device_authorization"] == 0
    assert cleanup_hub_auth(db_engine, commit=True, now=datetime.now(timezone.utc) + timedelta(days=2))["device_authorization"] == 1
