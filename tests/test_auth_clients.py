import base64
import hashlib
import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBasicCredentials
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

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
    {"type": "public"},
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
