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
)
import app.utils.auth_utils as auth_utils
from app.utils.hub_tokens import (
    InvalidHubGrant,
    hub_access_user,
    issue_hub_tokens,
    refresh_hub_tokens,
    revoke_hub_tokens,
)
from app.utils.hub_login import (
    HubLoginConfig, InvalidHubLogin, create_handoff_code, create_login,
    consume_login, exchange_handoff_code,
)


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)


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
            HubLoginTransaction.__table__, HubLoginCode.__table__,
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


def test_hub_and_legacy_credentials_both_authorize(engine, monkeypatch):
    monkeypatch.setattr(auth_utils, "engine", engine)
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
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3", "https://orcid.org")
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
    code, redirect = create_handoff_code(engine, transaction, "0000-0000-0000-0001", "Test", "Human", now=NOW)
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
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3", "https://orcid.org")
    state, browser_secret, _ = create_login(engine, config, "crucible-hub", config.callback_uri, "hub-state-123456789", "a" * 43, now=NOW)
    with pytest.raises(InvalidHubLogin):
        consume_login(engine, state, browser_secret, now=NOW + timedelta(minutes=10))

    second_state, second_browser_secret, _ = create_login(engine, config, "crucible-hub", config.callback_uri, "hub-state-123456789", "a" * 43, now=NOW)
    transaction = consume_login(engine, second_state, second_browser_secret, now=NOW)
    code, _ = create_handoff_code(engine, transaction, "0000-0000-0000-0001", None, None, now=NOW)
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(engine, code, "a" * 43, "crucible-hub", config.callback_uri, now=NOW + timedelta(minutes=1))


def test_browser_login_and_code_exchange_routes(engine, monkeypatch):
    import app.api.routes.hub_auth as routes
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "https://crucible.lbl.gov/hub/auth/callback")
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "http://localhost:7777")
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "test-only-secret")
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    monkeypatch.setattr(routes, "orcid_authorization_url", lambda config, state, nonce, max_age=None: f"https://orcid.org/oauth/authorize?state={state}")
    monkeypatch.setattr(routes, "exchange_orcid_code", lambda config, code, nonce: ("0000-0000-0000-0001", "Test", "Human", None))
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


def test_orcid_id_token_verification_rejects_wrong_identity_context(monkeypatch):
    import time
    from joserfc import jwt
    from joserfc.errors import JoseError
    from joserfc.jwk import RSAKey
    from app.utils.hub_orcid import verify_orcid_id_token

    monkeypatch.setattr("app.utils.hub_orcid.get_orcid_client_id", lambda: "test-orcid-client")
    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_ID", "test-orcid-client")
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3", "https://orcid.org")
    key = RSAKey.generate_key(auto_kid=True)
    jwks = {"keys": [key.as_dict()]}
    claims = {
        "iss": "https://orcid.org", "sub": "0000-0000-0000-0001",
        "aud": "test-orcid-client", "iat": int(time.time()),
        "exp": int(time.time()) + 60, "nonce": "expected-nonce",
    }
    encoded = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)
    assert verify_orcid_id_token(config, encoded, jwks, "expected-nonce")[0] == claims["sub"]
    with pytest.raises(JoseError):
        verify_orcid_id_token(config, encoded, jwks, "wrong-nonce")
    other_key = RSAKey.generate_key(auto_kid=True)
    with pytest.raises(JoseError):
        verify_orcid_id_token(config, encoded, {"keys": [other_key.as_dict()]}, "expected-nonce")
    for field, bad in (("iss", "https://sandbox.orcid.org"), ("aud", "other-client"), ("exp", int(time.time()) - 60)):
        rejected = jwt.encode({"alg": "RS256", "kid": key.kid}, {**claims, field: bad}, key)
        with pytest.raises(JoseError):
            verify_orcid_id_token(config, rejected, jwks, "expected-nonce")


def test_orcid_authorization_request_has_state_nonce_and_exact_callback(monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from app.utils.hub_orcid import orcid_authorization_url

    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_SECRET", "sandbox-secret")
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3", "https://orcid.org")
    url = orcid_authorization_url(config, "api-state", "api-nonce")
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https" and parsed.netloc == "orcid.org"
    assert params["redirect_uri"] == ["https://crucible.lbl.gov/api/v3/auth/hub/callback"]
    assert params["state"] == ["api-state"]
    assert params["nonce"] == ["api-nonce"]
    assert params["scope"] == ["openid"]
    assert params["client_id"] == ["sandbox-client"]


def test_orcid_auth_time_is_returned_and_future_values_are_ignored(monkeypatch):
    import time
    from joserfc import jwt
    from joserfc.jwk import RSAKey
    from app.utils.hub_orcid import authentication_time, verify_orcid_id_token

    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_ID", "test-orcid-client")
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3", "https://orcid.org")
    key = RSAKey.generate_key(auto_kid=True)
    signed_in = int(time.time()) - 5
    claims = {"iss": "https://orcid.org", "sub": "0000-0000-0000-0001", "aud": "test-orcid-client", "iat": int(time.time()), "exp": int(time.time()) + 60, "nonce": "n", "auth_time": signed_in}
    encoded = jwt.encode({"alg": "RS256", "kid": key.kid}, claims, key)
    assert verify_orcid_id_token(config, encoded, {"keys": [key.as_dict()]}, "n")[3] == datetime.fromtimestamp(signed_in, timezone.utc)
    assert authentication_time(None) is None
    assert authentication_time(True) is None
    assert authentication_time("123") is None
    assert authentication_time(int(time.time()) + 3600) is None


def test_orcid_reauthentication_request_forwards_max_age(monkeypatch):
    from urllib.parse import parse_qs, urlparse
    from app.utils.hub_orcid import orcid_authorization_url

    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_ID", "sandbox-client")
    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_SECRET", "sandbox-secret")
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3", "https://orcid.org")
    forced = parse_qs(urlparse(orcid_authorization_url(config, "s", "n", 0)).query)
    assert forced["max_age"] == ["0"] and forced["prompt"] == ["login"]
    bounded = parse_qs(urlparse(orcid_authorization_url(config, "s", "n", 300)).query)
    assert bounded["max_age"] == ["300"] and "prompt" not in bounded
    plain = parse_qs(urlparse(orcid_authorization_url(config, "s", "n")).query)
    assert "max_age" not in plain and "prompt" not in plain


def test_auth_time_flows_from_handoff_code_to_hub_principal(engine, monkeypatch):
    import base64
    import hashlib
    monkeypatch.setattr(auth_utils, "engine", engine)
    monkeypatch.setattr(auth_utils, "_PUBLIC_GROUP_ID", None)
    config = HubLoginConfig("crucible-hub", "https://crucible.lbl.gov/hub/auth/callback", "https://crucible.lbl.gov/api/v3", "https://orcid.org")
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    signed_in = datetime.now(timezone.utc) - timedelta(seconds=20)
    state, browser_secret, _ = create_login(engine, config, "crucible-hub", config.callback_uri, "hub-state-123456789", challenge)
    transaction = consume_login(engine, state, browser_secret)
    code, _ = create_handoff_code(engine, transaction, "0000-0000-0000-0001", None, None, auth_time=signed_in)
    pair = exchange_handoff_code(engine, code, verifier, "crucible-hub", config.callback_uri)
    with Session(engine) as session:
        assert session.exec(select(HubTokenFamily)).one().auth_time.replace(tzinfo=timezone.utc) == signed_in
    principal = auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=pair.access_token))
    assert principal.credential == "hub"
    assert principal.authenticated_at == signed_in
    refreshed = refresh_hub_tokens(engine, pair.refresh_token, "crucible-hub")
    assert auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=refreshed.access_token)).authenticated_at == signed_in


def test_login_accepts_only_an_exact_configured_callback(engine, monkeypatch):
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
        with pytest.raises(HubLoginUnavailable):
            load_login_config()
