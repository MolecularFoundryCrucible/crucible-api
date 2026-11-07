import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasicCredentials
from sqlalchemy.pool import StaticPool
from sqlalchemy import update
from sqlmodel import Session, SQLModel, create_engine, select

from app import config
from app.api.routes.hub_auth import require_hub_client
from app.auth.clients import registered_clients
from app.models.core_models import (
    AccessGroup, ApiKey, ApiToken, AuthEvent, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, User,
    UserAccessGroupLink, UserIdentity,
)
from app.config import ConfigurationError, OidcProvider, check_settings, get_secret, get_settings
from app.utils import auth_utils
from app.utils.hub_login import InvalidHubLogin, consume_login, create_handoff_code, create_login, exchange_handoff_code, load_login_config
from app.utils.hub_tokens import InvalidHubGrant, refresh_hub_tokens
from app.utils.permissions import API_KEY, SESSION, PlatformRole, PrivilegeMode, Principal, resolve_authorization_context

NOW = datetime.now(timezone.utc)
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
ORCID = OidcProvider(
    key="orcid", display_name="ORCID", issuer="https://orcid.org", client_id="APP-TEST", scopes="openid",
    token_endpoint_auth_method="client_secret_post", registration="open", legacy_callback=True,
)
HUB = {"key": "hub", "client_id": "crucible-hub", "display_name": "Crucible Hub", "type": "confidential", "redirect_uris": ["https://hub.example.org/auth/callback"]}
NOTES = {"key": "notes", "client_id": "lab-notes", "display_name": "Lab notes", "type": "confidential", "redirect_uris": ["http://localhost:8080/callback"]}
SECRETS = {"OAUTH_HUB_CLIENT_SECRET": "h" * 40, "OAUTH_NOTES_CLIENT_SECRET": "n" * 40}


@pytest.fixture
def db_engine(use_engine):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[
        User.__table__, AccessGroup.__table__, UserAccessGroupLink.__table__, ApiToken.__table__, ApiKey.__table__, AuthEvent.__table__,
        HubTokenFamily.__table__, HubRefreshToken.__table__, HubAccessToken.__table__, HubLoginTransaction.__table__, HubLoginCode.__table__, UserIdentity.__table__,
    ])
    with Session(engine) as session:
        session.add(User(first_name="Test", last_name="Human", unique_id="0000-0000-0000-0001"))
        session.commit()
    use_engine(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def two_clients(monkeypatch, orcid_provider_settings):
    orcid_provider_settings()
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps([HUB, NOTES]))
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "https://api.example.org")
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()


def basic(client_id, secret):
    return HTTPBasicCredentials(username=client_id, password=secret)


def signed_in(engine, client):
    login = load_login_config()
    state, browser_secret, _ = create_login(engine, login, client["client_id"], client["redirect_uris"][0], "client-state-123456789", CHALLENGE, now=NOW)
    code, _ = create_handoff_code(engine, consume_login(engine, state, browser_secret, now=NOW), ORCID, "0000-0000-0000-0001", "Test", "Human", now=NOW)
    return code


def test_each_client_authenticates_with_its_own_secret(two_clients):
    assert require_hub_client(basic("crucible-hub", SECRETS["OAUTH_HUB_CLIENT_SECRET"])) == "crucible-hub"
    assert require_hub_client(basic("lab-notes", SECRETS["OAUTH_NOTES_CLIENT_SECRET"])) == "lab-notes"
    for wrong in (basic("crucible-hub", SECRETS["OAUTH_NOTES_CLIENT_SECRET"]), basic("unknown", "x" * 40), None):
        with pytest.raises(HTTPException) as refused:
            require_hub_client(wrong)
        assert refused.value.status_code == 401


def test_a_redirect_must_be_on_that_clients_own_list(two_clients, db_engine):
    login = load_login_config()
    with pytest.raises(InvalidHubLogin):
        create_login(db_engine, login, "lab-notes", HUB["redirect_uris"][0], "client-state-123456789", CHALLENGE, now=NOW)
    with pytest.raises(InvalidHubLogin):
        create_login(db_engine, login, "nobody", HUB["redirect_uris"][0], "client-state-123456789", CHALLENGE, now=NOW)


def test_one_clients_code_and_session_are_useless_to_another(two_clients, db_engine):
    code = signed_in(db_engine, HUB)
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(db_engine, code, VERIFIER, "lab-notes", HUB["redirect_uris"][0], now=NOW)
    pair = exchange_handoff_code(db_engine, code, VERIFIER, "crucible-hub", HUB["redirect_uris"][0], now=NOW)
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(db_engine, pair.refresh_token, "lab-notes", now=NOW)
    principal = auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=pair.access_token))
    assert (principal.credential, principal.client_id) == (SESSION, "crucible-hub")


def test_removing_a_client_ends_its_sessions_at_once(two_clients, db_engine, monkeypatch):
    pair = exchange_handoff_code(db_engine, signed_in(db_engine, NOTES), VERIFIER, "lab-notes", NOTES["redirect_uris"][0], now=NOW)
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps([HUB]))
    config.get_settings.cache_clear()
    with pytest.raises(HTTPException) as refused:
        auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=pair.access_token))
    assert refused.value.status_code == 401


def test_the_older_hub_settings_alone_give_todays_client(monkeypatch, orcid_provider_settings):
    orcid_provider_settings()
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "http://localhost:3000/cb, https://hub.example.org/cb")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "s" * 40)
    config.get_settings.cache_clear()
    [client] = registered_clients()
    assert (client.client_id, client.redirect_uris, client.secret_name) == ("crucible-hub", ("http://localhost:3000/cb", "https://hub.example.org/cb"), "HUB_AUTH_CLIENT_SECRET")
    check_settings()


@pytest.mark.parametrize("change", [
    {"redirect_uris": ["https://hub.example.org/cb#fragment"]},
    {"redirect_uris": ["https://user:pw@hub.example.org/cb"]},
    {"redirect_uris": ["http://hub.example.org/cb"]},
    {"redirect_uris": ["https://hub.example.org/cb?x=1"]},
    {"redirect_uris": []},
    {"type": "native"},
    {"redirect_uris": ["http://127.0.0.1/callback"]},
    {"key": "Hub"},
    {"extra": True},
])
def test_unsafe_client_entries_are_refused(monkeypatch, change):
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps([{**HUB, **change}]))
    with pytest.raises(ConfigurationError, match="CRUCIBLE_AUTH_CLIENTS"):
        get_settings()


@pytest.mark.parametrize("field", ["key", "client_id"])
def test_a_client_listed_twice_is_refused(monkeypatch, field):
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps([HUB, {**NOTES, field: HUB[field]}]))
    with pytest.raises(ConfigurationError, match=f"same {field} twice"):
        get_settings()


def test_both_ways_of_naming_the_hub_are_refused(two_clients, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "https://hub.example.org/auth/callback")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "x" * 40)
    config.get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match="both set"):
        check_settings()


def test_a_clients_secret_must_be_given_and_long(two_clients, monkeypatch):
    monkeypatch.delenv("OAUTH_NOTES_CLIENT_SECRET")
    assert "OAUTH_NOTES_CLIENT_SECRET" in config.missing_required_settings()
    for name in ("OAUTH_NOTES_CLIENT_SECRET", "HUB_AUTH_CLIENT_SECRET"):
        monkeypatch.setenv(name, "n" * 31)
        config.get_settings.cache_clear()
        with pytest.raises(ConfigurationError, match=f"{name} must be at least 32 characters") as refused:
            get_secret(name)
        assert "n" * 31 not in str(refused.value)
        monkeypatch.setenv(name, "n" * 32)
        config.get_settings.cache_clear()
        assert get_secret(name) == "n" * 32


def test_only_api_keys_of_administrators_start_elevated():
    for credential, expected in ((API_KEY, PrivilegeMode.ELEVATED), (SESSION, PrivilegeMode.NORMAL)):
        actor = Principal(user_id="0000-0000-0000-0009", platform_role=PlatformRole.ADMIN, credential=credential, internal_user_id=9)
        assert resolve_authorization_context(actor, None).privilege_mode is expected


@pytest.fixture
def api(two_clients, db_engine):
    from fastapi.testclient import TestClient

    from app.api.main import app

    with TestClient(app, base_url="https://api.example.org") as client:
        yield client


HUB_AUTH = ("crucible-hub", SECRETS["OAUTH_HUB_CLIENT_SECRET"])
NOTES_AUTH = ("lab-notes", SECRETS["OAUTH_NOTES_CLIENT_SECRET"])


def exchange(api, code, auth=HUB_AUTH, **changes):
    form = {"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "redirect_uri": HUB["redirect_uris"][0], **changes}
    return api.post("/oauth/token", auth=auth, data={key: value for key, value in form.items() if value is not None})


def test_standard_code_exchange_works_once(api, db_engine):
    code = signed_in(db_engine, HUB)
    first = exchange(api, code)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["token_type"] == "Bearer" and 0 < body["expires_in"] <= 900 and body["refresh_token"].startswith("chbr_")
    assert first.headers["cache-control"] == "no-store"
    assert (exchange(api, code).status_code, exchange(api, code).json()["error"]) == (400, "invalid_grant")
    principal = auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=body["access_token"]))
    assert principal.client_id == "crucible-hub"


@pytest.mark.parametrize("changes,error", [
    ({"code_verifier": None}, "invalid_request"),
    ({"code_verifier": "w" * 43}, "invalid_grant"),
    ({"redirect_uri": "https://hub.example.org/other"}, "invalid_grant"),
    ({"grant_type": "password"}, "unsupported_grant_type"),
])
def test_a_code_needs_its_verifier_and_redirect(api, db_engine, changes, error):
    code = signed_in(db_engine, HUB)
    refused = exchange(api, code, **changes)
    assert (refused.status_code, refused.json()["error"]) == (400, error)
    assert exchange(api, code).status_code == 200


def test_repeated_parameters_are_refused(api, db_engine):
    code = signed_in(db_engine, HUB)
    form = [("grant_type", "authorization_code"), ("code", code), ("code_verifier", VERIFIER), ("code_verifier", VERIFIER), ("redirect_uri", HUB["redirect_uris"][0])]
    from urllib.parse import urlencode

    refused = api.post("/oauth/token", auth=HUB_AUTH, content=urlencode(form), headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert (refused.status_code, refused.json()["error"]) == (400, "invalid_request")


def test_wrong_client_is_invalid_client_and_another_clients_code_is_useless(api, db_engine):
    code = signed_in(db_engine, HUB)
    assert exchange(api, code, auth=("crucible-hub", "x" * 40)).json()["error"] == "invalid_client"
    assert exchange(api, code, auth=NOTES_AUTH).json()["error"] == "invalid_grant"
    assert exchange(api, code).status_code == 200


def past_the_grace(engine):
    with Session(engine) as session:
        session.execute(update(HubRefreshToken).where(HubRefreshToken.spent_at.is_not(None)).values(spent_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
        session.commit()


def test_refresh_rotates_and_reuse_ends_the_session(api, db_engine):
    tokens = exchange(api, signed_in(db_engine, HUB)).json()
    assert api.post("/oauth/token", auth=NOTES_AUTH, data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}).json()["error"] == "invalid_grant"
    second = api.post("/oauth/token", auth=HUB_AUTH, data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert second.status_code == 200 and second.json()["refresh_token"] != tokens["refresh_token"]
    past_the_grace(db_engine)
    reused = api.post("/oauth/token", auth=HUB_AUTH, data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]})
    assert reused.json()["error"] == "invalid_grant"
    later = api.post("/oauth/token", auth=HUB_AUTH, data={"grant_type": "refresh_token", "refresh_token": second.json()["refresh_token"]})
    assert later.json()["error"] == "invalid_grant"


def test_revocation_answers_alike_for_unknown_and_other_clients_tokens(api, db_engine):
    tokens = exchange(api, signed_in(db_engine, HUB)).json()
    for auth, token in ((HUB_AUTH, "chbr_unknown"), (NOTES_AUTH, tokens["refresh_token"])):
        answer = api.post("/oauth/revoke", auth=auth, data={"token": token})
        assert (answer.status_code, answer.json()) == (200, {})
    assert auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=tokens["access_token"])).client_id == "crucible-hub"
    assert api.post("/oauth/revoke", auth=HUB_AUTH, data={"token": tokens["refresh_token"]}).status_code == 200
    with pytest.raises(HTTPException):
        auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=tokens["access_token"]))


def test_errors_carry_no_internal_details(api, db_engine):
    refused = exchange(api, "not-a-code")
    assert set(refused.json()) <= {"error", "error_description"}
    assert "Traceback" not in refused.text and "hublogincode" not in refused.text


def test_the_hub_routes_and_the_standard_route_share_one_server(api, db_engine):
    via_hub = api.post("/auth/hub/token", auth=HUB_AUTH, json={"code": signed_in(db_engine, HUB), "code_verifier": VERIFIER, "redirect_uri": HUB["redirect_uris"][0]})
    assert via_hub.status_code == 200, via_hub.text
    refreshed = api.post("/oauth/token", auth=HUB_AUTH, data={"grant_type": "refresh_token", "refresh_token": via_hub.json()["refresh_token"]})
    assert refreshed.status_code == 200
    past_the_grace(db_engine)
    again = api.post("/auth/hub/refresh", auth=HUB_AUTH, json={"refresh_token": via_hub.json()["refresh_token"]})
    assert again.status_code == 401


CLI = {"key": "nanocrucible", "client_id": "nano-crucible", "display_name": "nano-crucible", "type": "public", "redirect_uris": ["http://127.0.0.1/callback", "http://[::1]/callback"]}


@pytest.fixture
def with_cli(two_clients, monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps([HUB, NOTES, CLI]))
    config.get_settings.cache_clear()


def test_a_public_client_needs_no_secret(with_cli):
    assert "OAUTH_NANOCRUCIBLE_CLIENT_SECRET" not in config.missing_required_settings()
    check_settings()


@pytest.mark.parametrize("uri,allowed", [
    ("http://127.0.0.1:53817/callback", True), ("http://[::1]:8080/callback", True), ("http://127.0.0.1/callback", True),
    ("http://127.0.0.1:53817/other", False), ("http://localhost:53817/callback", False), ("http://127.0.0.2:53817/callback", False),
    ("https://127.0.0.1:53817/callback", False), ("http://127.0.0.1:53817/callback?x=1", False), ("http://user@127.0.0.1:53817/callback", False),
])
def test_public_loopback_redirects_accept_any_port_and_nothing_else(with_cli, uri, allowed):
    from app.auth.clients import client_by_id

    assert client_by_id("nano-crucible").allows_redirect(uri) is allowed
    assert client_by_id("crucible-hub").allows_redirect("https://hub.example.org:8443/auth/callback") is False


def test_public_client_exchanges_its_code_with_pkce_and_no_secret(with_cli, api, db_engine):
    loopback = {**CLI, "redirect_uris": ["http://127.0.0.1:53817/callback"]}
    code = signed_in(db_engine, loopback)
    form = {"grant_type": "authorization_code", "client_id": "nano-crucible", "code": code, "redirect_uri": "http://127.0.0.1:53817/callback"}
    missing = api.post("/oauth/token", data=form)
    assert (missing.status_code, missing.json()["error"]) == (400, "invalid_request")
    tokens = api.post("/oauth/token", data={**form, "code_verifier": VERIFIER})
    assert tokens.status_code == 200, tokens.text
    refreshed = api.post("/oauth/token", data={"grant_type": "refresh_token", "client_id": "nano-crucible", "refresh_token": tokens.json()["refresh_token"]})
    assert refreshed.status_code == 200
    assert api.post("/oauth/revoke", data={"client_id": "nano-crucible", "token": refreshed.json()["refresh_token"]}).status_code == 200


def test_public_client_cannot_use_the_hubs_secret_routes_or_a_secret(with_cli, api, db_engine):
    code = signed_in(db_engine, {**CLI, "redirect_uris": ["http://127.0.0.1:53817/callback"]})
    assert api.post("/auth/hub/token", auth=("nano-crucible", "x" * 40), json={"code": code, "code_verifier": VERIFIER, "redirect_uri": "http://127.0.0.1:53817/callback"}).status_code == 401
    assert api.post("/oauth/token", auth=("nano-crucible", "x" * 40), data={"grant_type": "authorization_code", "code": code, "code_verifier": VERIFIER, "redirect_uri": "http://127.0.0.1:53817/callback"}).json()["error"] == "invalid_client"
    assert api.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "crucible-hub", "code": signed_in(db_engine, HUB), "code_verifier": VERIFIER, "redirect_uri": HUB["redirect_uris"][0]}).json()["error"] == "invalid_client"


def test_public_sessions_last_longer_and_their_codes_wait_for_the_confirmation(with_cli, db_engine):
    from datetime import timedelta

    from app.utils.hub_login import code_lifetime
    from app.utils.hub_tokens import issue_hub_tokens, session_lifetimes

    assert session_lifetimes("nano-crucible") == (timedelta(days=90), timedelta(days=14))
    assert session_lifetimes("crucible-hub") == (timedelta(days=30), timedelta(days=7))
    assert (code_lifetime("nano-crucible"), code_lifetime("crucible-hub")) == (timedelta(minutes=5), timedelta(minutes=1))
    with Session(db_engine) as session:
        user_id = session.exec(select(User.id)).first()
    pair = issue_hub_tokens(db_engine, user_id, "nano-crucible", now=NOW)
    assert abs((pair.refresh_expires_at - NOW) - timedelta(days=90)) < timedelta(seconds=1)


def test_metadata_offers_no_secret_for_public_clients(with_cli, api):
    metadata = api.get("/.well-known/oauth-authorization-server").json()
    assert metadata["token_endpoint_auth_methods_supported"] == ["client_secret_basic", "none"]
