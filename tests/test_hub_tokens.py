from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.api.dependencies import get_engine
from app.api.routes.hub_auth import router as hub_auth_router
from app.models.core_models import (
    AccessGroup,
    ApiToken,
    HubAccessToken,
    HubLoginCode,
    HubLoginTransaction,
    HubRefreshToken,
    HubTokenFamily,
    User,
    UserAccessGroupLink,
    UserIdentity,
)
import app.utils.auth_utils as auth_utils
from app.utils.hub_tokens import (
    InvalidHubGrant,
    hub_access_user,
    issue_hub_tokens,
    refresh_hub_tokens,
    revoke_hub_tokens,
)
from app.auth.providers import VerifiedIdentity
from app.config import OidcProvider
from app.utils.hub_login import (
    HubLoginConfig, InvalidHubLogin, create_handoff_code, create_login,
    consume_login, exchange_handoff_code,
)


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


ORCID_PROVIDER = OidcProvider(
    key="orcid", display_name="ORCID", issuer="https://orcid.org", client_id="APP-TEST", scopes="openid",
    token_endpoint_auth_method="client_secret_post", registration="open", legacy_callback=True,
)


def test_cleanup_preserves_refresh_replay_history_and_defaults_to_dry_run(engine):
    from app.utils.hub_auth_maintenance import cleanup_hub_auth

    first = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    refresh_hub_tokens(engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=1))
    preview = cleanup_hub_auth(engine, now=NOW + timedelta(days=31))
    assert preview["hubtokenfamily"] == 1
    with Session(engine) as session:
        assert len(session.exec(select(HubRefreshToken)).all()) == 2
    cleanup_hub_auth(engine, commit=True, now=NOW + timedelta(days=1))
    with Session(engine) as session:
        assert session.exec(select(HubAccessToken)).all() == []
        assert len(session.exec(select(HubRefreshToken)).all()) == 2
    cleanup_hub_auth(engine, commit=True, now=NOW + timedelta(days=31))
    with Session(engine) as session:
        assert session.exec(select(HubTokenFamily)).all() == []
        assert session.exec(select(HubRefreshToken)).all() == []


def access_user(engine, token, *, now):
    with Session(engine) as session:
        return hub_access_user(session, token, now=now)


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(
        engine,
        tables=[
            User.__table__, AccessGroup.__table__, UserAccessGroupLink.__table__, ApiToken.__table__,
            HubTokenFamily.__table__, HubRefreshToken.__table__, HubAccessToken.__table__,
            HubLoginTransaction.__table__, HubLoginCode.__table__, UserIdentity.__table__,
        ],
    )
    with Session(engine) as session:
        session.add(User(first_name="Test", last_name="Human", unique_id="0000-0000-0000-0001"))
        session.add(User(first_name="Test", last_name="Service", unique_id="sa-test", is_service_account=True))
        session.commit()
    yield engine
    engine.dispose()


def test_issue_refresh_and_access_expiry(engine):
    first = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    assert access_user(engine, first.access_token, now=NOW) == first.user_unique_id
    with pytest.raises(InvalidHubGrant):
        access_user(engine, first.access_token, now=NOW + timedelta(minutes=15))

    second = refresh_hub_tokens(engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=14))
    assert first.refresh_token != second.refresh_token
    assert first.access_token != second.access_token
    assert second.refresh_expires_at == first.refresh_expires_at
    assert access_user(engine, first.access_token, now=NOW + timedelta(minutes=14)) == first.user_unique_id
    assert access_user(engine, second.access_token, now=NOW + timedelta(minutes=15)) == first.user_unique_id


def test_issue_and_refresh_with_foreign_keys_enforced():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    SQLModel.metadata.create_all(
        engine,
        tables=[User.__table__, HubTokenFamily.__table__, HubRefreshToken.__table__, HubAccessToken.__table__],
    )
    try:
        with Session(engine) as session:
            session.add(User(first_name="Test", last_name="Human", unique_id="0000-0000-0000-0001"))
            session.commit()
        first = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
        second = refresh_hub_tokens(engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=1))
        assert access_user(engine, second.access_token, now=NOW + timedelta(minutes=1)) == first.user_unique_id
    finally:
        engine.dispose()


def test_spent_refresh_revokes_family(engine):
    first = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    second = refresh_hub_tokens(engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=1))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=2))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(engine, second.refresh_token, "hub", now=NOW + timedelta(minutes=3))
    with pytest.raises(InvalidHubGrant):
        access_user(engine, second.access_token, now=NOW + timedelta(minutes=3))


def test_client_binding_and_expiry(engine):
    pair = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(engine, pair.refresh_token, "other", now=NOW + timedelta(minutes=1))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(engine, pair.refresh_token, "hub", now=NOW + timedelta(days=7))
    with pytest.raises(InvalidHubGrant):
        access_user(engine, pair.access_token, now=NOW + timedelta(days=7))

    second = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    current = second
    for day in range(1, 30):
        current = refresh_hub_tokens(engine, current.refresh_token, "hub", now=NOW + timedelta(days=day))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(engine, current.refresh_token, "hub", now=NOW + timedelta(days=30))


def test_revoke_and_service_accounts(engine):
    with pytest.raises(InvalidHubGrant):
        issue_hub_tokens(engine, "sa-test", "hub", now=NOW)
    pair = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    revoke_hub_tokens(engine, pair.refresh_token, "other", now=NOW + timedelta(minutes=1))
    assert access_user(engine, pair.access_token, now=NOW + timedelta(minutes=1)) == pair.user_unique_id
    revoke_hub_tokens(engine, pair.access_token, "hub", now=NOW + timedelta(minutes=2))
    revoke_hub_tokens(engine, pair.access_token, "hub", now=NOW + timedelta(minutes=2))
    with pytest.raises(InvalidHubGrant):
        access_user(engine, pair.access_token, now=NOW + timedelta(minutes=2))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(engine, pair.refresh_token, "hub", now=NOW + timedelta(minutes=2))


def test_logout_with_rotated_token_revokes_only_its_family(engine):
    first = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    other = issue_hub_tokens(engine, first.user_unique_id, "hub", now=NOW)
    second = refresh_hub_tokens(engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=1))
    revoke_hub_tokens(engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=2))
    for pair in (first, second):
        with pytest.raises(InvalidHubGrant):
            access_user(engine, pair.access_token, now=NOW + timedelta(minutes=2))
        with pytest.raises(InvalidHubGrant):
            refresh_hub_tokens(engine, pair.refresh_token, "hub", now=NOW + timedelta(minutes=2))
    assert access_user(engine, other.access_token, now=NOW + timedelta(minutes=2)) == other.user_unique_id
    refresh_hub_tokens(engine, other.refresh_token, "hub", now=NOW + timedelta(minutes=2))


def test_tokens_are_stored_hashed(engine):
    pair = issue_hub_tokens(engine, "0000-0000-0000-0001", "hub", now=NOW)
    with Session(engine) as session:
        access = session.exec(select(HubAccessToken)).one()
        refresh = session.exec(select(HubRefreshToken)).one()
        assert pair.access_token not in access.token_hash
        assert pair.refresh_token not in refresh.token_hash
        assert len(access.token_hash) == 64
        assert len(refresh.token_hash) == 64


def test_refresh_and_revoke_routes(engine, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "test-only-secret")
    app = FastAPI()
    app.include_router(hub_auth_router)
    app.dependency_overrides[get_engine] = lambda: engine
    pair = issue_hub_tokens(engine, "0000-0000-0000-0001", "crucible-hub")
    with TestClient(app) as client:
        for auth in (None, ("crucible-hub", "wrong-secret")):
            denied_revoke = client.post("/auth/hub/revoke", auth=auth, json={"token": pair.refresh_token})
            assert denied_revoke.status_code == 401
        denied = client.post("/auth/hub/refresh", json={"refresh_token": pair.refresh_token})
        assert denied.status_code == 401

        refreshed = client.post(
            "/auth/hub/refresh",
            auth=("crucible-hub", "test-only-secret"),
            json={"refresh_token": pair.refresh_token},
        )
        assert refreshed.status_code == 200
        assert refreshed.headers["cache-control"] == "no-store"
        next_pair = refreshed.json()
        assert next_pair["refresh_token"] != pair.refresh_token

        revoked = client.post(
            "/auth/hub/revoke",
            auth=("crucible-hub", "test-only-secret"),
            json={"token": pair.refresh_token},
        )
        assert revoked.status_code == 204
        assert revoked.content == b""
        assert revoked.headers["cache-control"] == "no-store"
        for token in (pair.refresh_token, "chbr_" + "x" * 64):
            assert client.post(
                "/auth/hub/revoke", auth=("crucible-hub", "test-only-secret"), json={"token": token},
            ).status_code == 204
        failed = client.post(
            "/auth/hub/refresh",
            auth=("crucible-hub", "test-only-secret"),
            json={"refresh_token": next_pair["refresh_token"]},
        )
        assert failed.status_code == 401


def test_hub_and_legacy_credentials_both_authorize(use_engine, engine, monkeypatch):
    use_engine(engine)
    monkeypatch.setattr(auth_utils, "_PUBLIC_GROUP_ID", None)
    pair = issue_hub_tokens(engine, "0000-0000-0000-0001", "crucible-hub")
    legacy_key = "a" * 64
    with Session(engine) as session:
        session.add(
            ApiToken(
                token=legacy_key,
                user_unique_id="0000-0000-0000-0001",
                access_group_id="0000-0000-0000-0001",
                token_created=datetime.now(timezone.utc).date().isoformat(),
            )
        )
        session.commit()
    for token in (pair.access_token, legacy_key):
        principal = auth_utils._authorize_user(
            HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
        )
        assert principal.user_id == "0000-0000-0000-0001"
    revoke_hub_tokens(engine, pair.refresh_token, "crucible-hub")
    assert auth_utils._authorize_user(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials=legacy_key)
    ).user_id == "0000-0000-0000-0001"


def test_login_handoff_is_bound_and_single_use(engine):
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3")
    verifier = "v" * 43
    import base64
    import hashlib
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, browser_secret, _ = create_login(engine, config, "crucible-hub", config.callback_uri, "hub-state-123456789", challenge, now=NOW)
    with pytest.raises(InvalidHubLogin):
        consume_login(engine, state, "wrong-browser", now=NOW)
    transaction = consume_login(engine, state, browser_secret, now=NOW)
    with pytest.raises(InvalidHubLogin):
        consume_login(engine, state, browser_secret, now=NOW)
    code, redirect = create_handoff_code(engine, transaction, ORCID_PROVIDER, "0000-0000-0000-0001", "Test", "Human", now=NOW)
    assert "state=hub-state-123456789" in redirect
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(engine, code, "w" * 43, "crucible-hub", config.callback_uri, now=NOW)
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(engine, code, verifier, "other", config.callback_uri, now=NOW)
    pair = exchange_handoff_code(engine, code, verifier, "crucible-hub", config.callback_uri, now=NOW)
    assert pair.user_unique_id == "0000-0000-0000-0001"
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(engine, code, verifier, "crucible-hub", config.callback_uri, now=NOW)


def test_login_expiry(engine):
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3")
    state, browser_secret, _ = create_login(engine, config, "crucible-hub", config.callback_uri, "hub-state-123456789", "a" * 43, now=NOW)
    with pytest.raises(InvalidHubLogin):
        consume_login(engine, state, browser_secret, now=NOW + timedelta(minutes=10))

    second_state, second_browser_secret, _ = create_login(engine, config, "crucible-hub", config.callback_uri, "hub-state-123456789", "a" * 43, now=NOW)
    transaction = consume_login(engine, second_state, second_browser_secret, now=NOW)
    code, _ = create_handoff_code(engine, transaction, ORCID_PROVIDER, "0000-0000-0000-0001", None, None, now=NOW)
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(engine, code, "a" * 43, "crucible-hub", config.callback_uri, now=NOW + timedelta(minutes=1))


def test_browser_login_and_code_exchange_routes(engine, monkeypatch, orcid_provider_settings):
    import app.api.routes.hub_auth as routes
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "https://crucible.lbl.gov/hub/auth/callback")
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "http://localhost:7777")
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "test-only-secret")
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    orcid_provider_settings()

    async def authorization_url(provider, callback_uri, state, nonce, max_age=None, code_verifier=None):
        return f"https://orcid.org/oauth/authorize?state={state}"

    async def verified_identity(provider, callback_uri, code, nonce, code_verifier=None):
        return VerifiedIdentity("0000-0000-0000-0001", "Test", "Human", None)

    async def new_code_verifier(provider):
        return None

    monkeypatch.setattr(routes, "authorization_url", authorization_url)
    monkeypatch.setattr(routes, "new_code_verifier", new_code_verifier)
    monkeypatch.setattr(routes, "verified_identity", verified_identity)
    app = FastAPI()
    app.include_router(hub_auth_router)
    app.dependency_overrides[get_engine] = lambda: engine
    verifier = "v" * 43
    import base64
    import hashlib
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    with TestClient(app) as client:
        rejected = client.get("/auth/hub/callback", params={"code": "orcid-code", "state": "wrong-state"}, follow_redirects=False)
        assert rejected.status_code == 400
        started = client.get("/auth/hub/login", params={
            "client_id": "crucible-hub",
            "redirect_uri": "https://crucible.lbl.gov/hub/auth/callback",
            "state": "hub-state-123456789",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }, follow_redirects=False)
        assert started.status_code == 302
        assert started.headers["location"].startswith("https://orcid.org/")
        state = started.headers["location"].split("state=")[1]
        callback = client.get("/auth/hub/callback", params={"code": "orcid-code", "state": state}, follow_redirects=False)
        assert callback.status_code == 302
        from urllib.parse import parse_qs, urlparse
        code = parse_qs(urlparse(callback.headers["location"]).query)["code"][0]
        exchanged = client.post("/auth/hub/token", auth=("crucible-hub", "test-only-secret"), json={
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": "https://crucible.lbl.gov/hub/auth/callback",
        })
        assert exchanged.status_code == 200
        assert exchanged.json()["user_unique_id"] == "0000-0000-0000-0001"


@pytest.fixture
def hub_routes(engine, monkeypatch, orcid_provider_settings):
    import httpx
    import app.api.routes.hub_auth as routes
    from tests.test_auth_providers import FakeProvider

    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "https://crucible.lbl.gov/hub/auth/callback")
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "http://localhost:7777")
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "test-only-secret")
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    orcid_provider_settings()
    fake = FakeProvider()

    async def handle(self, request):
        await request.aread()
        return fake.respond(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    app = FastAPI()
    app.include_router(hub_auth_router)
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as client:
        yield client, fake


HUB_LOGIN = {
    "client_id": "crucible-hub",
    "redirect_uri": "https://crucible.lbl.gov/hub/auth/callback",
    "state": "hub-state-123456789",
    "code_challenge": "a" * 43,
    "code_challenge_method": "S256",
}


def start_hub_login(client, fake):
    from urllib.parse import parse_qs, urlparse

    started = client.get("/auth/hub/login", params=HUB_LOGIN, follow_redirects=False)
    assert started.status_code == 302
    query = parse_qs(urlparse(started.headers["location"]).query)
    assert query["redirect_uri"] == ["http://localhost:7777/auth/hub/callback"]
    fake.claims = {"nonce": query["nonce"][0]}
    return query["state"][0]


def test_hub_sign_in_runs_through_the_listed_provider(hub_routes):
    client, fake = hub_routes
    state = start_hub_login(client, fake)
    finished = client.get("/auth/hub/callback", params={"code": "orcid-code", "state": state}, follow_redirects=False)
    assert finished.status_code == 302
    assert finished.headers["location"].startswith("https://crucible.lbl.gov/hub/auth/callback?code=")


def hub_error_of(response):
    from urllib.parse import parse_qs, urlparse

    assert response.status_code == 302
    return parse_qs(urlparse(response.headers["location"]).query)["error"][0]


def test_hub_callback_refuses_a_token_for_the_wrong_nonce(hub_routes):
    client, fake = hub_routes
    state = start_hub_login(client, fake)
    fake.claims = {"nonce": "another"}
    assert hub_error_of(client.get("/auth/hub/callback", params={"code": "orcid-code", "state": state}, follow_redirects=False)) == "server_error"


def test_hub_callback_tells_the_hub_when_the_provider_is_down(hub_routes):
    client, fake = hub_routes
    state = start_hub_login(client, fake)
    fake.down = True
    assert hub_error_of(client.get("/auth/hub/callback", params={"code": "orcid-code", "state": state}, follow_redirects=False)) == "temporarily_unavailable"


def test_hub_login_tells_the_hub_when_discovery_fails(hub_routes):
    client, fake = hub_routes
    fake.down = True
    assert hub_error_of(client.get("/auth/hub/login", params=HUB_LOGIN, follow_redirects=False)) == "temporarily_unavailable"


def test_hub_login_is_503_without_a_provider_list(hub_routes, monkeypatch):
    from app.config import get_settings

    client, _ = hub_routes
    monkeypatch.delenv("CRUCIBLE_AUTH_PROVIDERS")
    get_settings.cache_clear()
    assert client.get("/auth/hub/login", params=HUB_LOGIN, follow_redirects=False).status_code == 503


def test_auth_time_flows_from_handoff_code_to_hub_principal(use_engine, engine, monkeypatch):
    import base64
    import hashlib
    use_engine(engine)
    monkeypatch.setattr(auth_utils, "_PUBLIC_GROUP_ID", None)
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3")
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    signed_in = datetime.now(timezone.utc) - timedelta(seconds=20)
    state, browser_secret, _ = create_login(engine, config, "crucible-hub", config.callback_uri, "hub-state-123456789", challenge)
    transaction = consume_login(engine, state, browser_secret)
    code, _ = create_handoff_code(engine, transaction, ORCID_PROVIDER, "0000-0000-0000-0001", None, None, auth_time=signed_in)
    pair = exchange_handoff_code(engine, code, verifier, "crucible-hub", config.callback_uri)
    with Session(engine) as session:
        assert session.exec(select(HubTokenFamily)).one().auth_time.replace(tzinfo=timezone.utc) == signed_in
    principal = auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=pair.access_token))
    assert principal.credential == "hub"
    assert principal.authenticated_at == signed_in
    refreshed = refresh_hub_tokens(engine, pair.refresh_token, "crucible-hub")
    assert auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=refreshed.access_token)).authenticated_at == signed_in


def test_login_accepts_only_an_exact_configured_callback(engine, monkeypatch, orcid_provider_settings):
    from app.config import get_settings
    orcid_provider_settings()
    from app.utils.hub_login import HubLoginUnavailable, load_login_config
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "test-only-secret")
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "https://crucible.lbl.gov/testapi-staging")
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "http://localhost:3000/hub/auth/callback, https://hub.example.org/hub/auth/callback")
    config = load_login_config()
    assert config.allowed_callbacks == ("http://localhost:3000/hub/auth/callback", "https://hub.example.org/hub/auth/callback")
    for callback in config.allowed_callbacks:
        create_login(engine, config, "crucible-hub", callback, "hub-state-123456789", "a" * 43)
    for callback in ("http://localhost:3001/hub/auth/callback", "https://hub.example.org/hub/auth/callback/x", "http://localhost:3000/hub/auth/callback, https://hub.example.org/hub/auth/callback"):
        with pytest.raises(InvalidHubLogin):
            create_login(engine, config, "crucible-hub", callback, "hub-state-123456789", "a" * 43)
    for configured in ("", " , ", "http://localhost:3000/cb,http://evil.example/cb"):
        monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", configured)
        get_settings.cache_clear()
        with pytest.raises(HubLoginUnavailable):
            load_login_config()
