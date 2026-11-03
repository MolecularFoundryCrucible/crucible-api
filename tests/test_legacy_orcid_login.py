from urllib.parse import parse_qs, urlparse

import pytest
import requests
from fastapi.testclient import TestClient

import app.api.routes.auth as auth_routes
import app.auth.oidc as oidc
import app.utils.auth_utils as auth_utils
from app.api.main import app
from app.auth.oidc import VerifiedIdentity

SIGNED_IN = VerifiedIdentity(issuer="https://orcid.org", subject="0000-0001", given_name="Ada", family_name=None, auth_time=None)


def start_login(client) -> str:
    response = client.get("/auth/login", follow_redirects=False)
    client.cookies.set("crucible_login_state", response.cookies["crucible_login_state"])
    return parse_qs(urlparse(response.headers["location"]).query)["state"][0]


@pytest.fixture(autouse=True)
def orcid_client(monkeypatch):
    monkeypatch.setenv("ORCID_CLIENT_ID", "test-client")
    monkeypatch.setenv("ORCID_CLIENT_SECRET", "test-secret")


@pytest.mark.parametrize("cookie, target", [
    (None, "/"),
    ("None", "/"),
    ("/api/v3/user_apikey", "/api/v3/user_apikey"),
    ("https://evil.example/phish", "/"),
    ("//evil.example/phish", "/"),
    ("/\\evil.example", "/"),
])
def test_callback_only_returns_to_local_paths(client, monkeypatch, cookie, target):
    monkeypatch.setattr(oidc, "exchange_code", lambda *args, **kwargs: SIGNED_IN)
    state = start_login(client)
    if cookie is not None:
        client.cookies.set("after_login_return_to_page", cookie)
    response = client.get("/auth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == target


def test_orcid_failure_is_502(client, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout("slow")
    monkeypatch.setattr(oidc, "exchange_code", fail)
    state = start_login(client)
    assert client.get("/auth/callback", params={"code": "abc", "state": state}, follow_redirects=False).status_code == 502


def test_invalid_token_from_orcid_is_502(client, monkeypatch):
    def fail(*args, **kwargs):
        raise oidc.InvalidSignIn
    monkeypatch.setattr(oidc, "exchange_code", fail)
    state = start_login(client)
    response = client.get("/auth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    assert response.status_code == 502
    assert any(c.startswith("crucible_login_state=") and "max-age=0" in c.lower() for c in response.headers.get_list("set-cookie"))


def test_login_sends_a_fresh_state_and_nonce(client):
    first = parse_qs(urlparse(client.get("/auth/login", follow_redirects=False).headers["location"]).query)
    second = parse_qs(urlparse(client.get("/auth/login", follow_redirects=False).headers["location"]).query)
    assert first["state"][0] != second["state"][0]
    assert first["nonce"][0] != second["nonce"][0]
    assert first["nonce"][0] != "whatever"


def test_login_keeps_the_registered_redirect_address(client):
    query = parse_qs(urlparse(client.get("/auth/login", follow_redirects=False).headers["location"]).query)
    assert query["redirect_uri"][0].endswith("/auth")
    assert query["client_id"] == ["test-client"]
    assert query["scope"] == ["openid"]


def test_login_state_cookie_is_protected(client):
    cookie = client.get("/auth/login", follow_redirects=False).headers["set-cookie"].lower()
    assert "crucible_login_state=" in cookie
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "max-age=600" in cookie


@pytest.mark.parametrize("path", ["/auth/callback", "/auth"])
def test_callback_refuses_missing_or_forged_state_before_calling_orcid(client, monkeypatch, path):
    called = []
    monkeypatch.setattr(oidc, "exchange_code", lambda *args, **kwargs: called.append(1))
    assert client.get(path, params={"code": "abc"}, follow_redirects=False).status_code == 400
    assert client.get(path, params={"code": "abc", "state": "forged"}, follow_redirects=False).status_code == 400
    start_login(client)
    assert client.get(path, params={"code": "abc", "state": "forged"}, follow_redirects=False).status_code == 400
    assert client.get(path, params={"code": "abc"}, follow_redirects=False).status_code == 400
    assert not called


def test_callback_refuses_a_state_from_another_browser(client, monkeypatch):
    called = []
    monkeypatch.setattr(oidc, "exchange_code", lambda *args, **kwargs: called.append(1))
    with TestClient(app) as attacker:
        state = start_login(attacker)
    start_login(client)
    assert client.get("/auth", params={"code": "abc", "state": state}, follow_redirects=False).status_code == 400
    assert not called


def test_callback_refuses_an_expired_or_tampered_state_cookie(client, monkeypatch):
    import time

    called = []
    monkeypatch.setattr(oidc, "exchange_code", lambda *args, **kwargs: called.append(1))
    expired = auth_utils.encode_token({"state": "s", "nonce": "n", "exp": int(time.time()) - 5})
    client.cookies.set("crucible_login_state", expired)
    assert client.get("/auth", params={"code": "abc", "state": "s"}, follow_redirects=False).status_code == 400
    forged = auth_utils.encode_token({"state": "s", "nonce": "n", "exp": int(time.time()) + 60}, secret_key="x" * 32)
    client.cookies.set("crucible_login_state", forged)
    assert client.get("/auth", params={"code": "abc", "state": "s"}, follow_redirects=False).status_code == 400
    assert not called


def test_provider_error_is_400_without_calling_orcid(client, monkeypatch):
    called = []
    monkeypatch.setattr(oidc, "exchange_code", lambda *args, **kwargs: called.append(1))
    state = start_login(client)
    assert client.get("/auth", params={"error": "access_denied", "state": state}, follow_redirects=False).status_code == 400
    assert not called


def test_state_cookie_is_cleared_after_callback(client, monkeypatch):
    monkeypatch.setattr(oidc, "exchange_code", lambda *args, **kwargs: SIGNED_IN)
    state = start_login(client)
    response = client.get("/auth", params={"code": "abc", "state": state}, follow_redirects=False)
    cleared = [c for c in response.headers.get_list("set-cookie") if c.startswith("crucible_login_state=")]
    assert cleared and "max-age=0" in cleared[0].lower()


def test_nonce_and_redirect_reach_token_verification(client, monkeypatch):
    seen = {}
    def exchange(oidc_client, code, nonce, **kwargs):
        seen.update(client=oidc_client, code=code, nonce=nonce)
        return SIGNED_IN
    monkeypatch.setattr(oidc, "exchange_code", exchange)
    response = client.get("/auth/login", follow_redirects=False)
    client.cookies.set("crucible_login_state", response.cookies["crucible_login_state"])
    query = parse_qs(urlparse(response.headers["location"]).query)
    client.get("/auth", params={"code": "abc", "state": query["state"][0]}, follow_redirects=False)
    assert seen["code"] == "abc"
    assert seen["nonce"] == query["nonce"][0]
    assert seen["client"].issuer == "https://orcid.org"
    assert seen["client"].redirect_uri == query["redirect_uri"][0]


@pytest.mark.parametrize("path", ["/user_apikey", "/auth/apikey"])
@pytest.mark.parametrize("token", [None, "expired-or-forged"])
def test_missing_or_invalid_session_redirects_to_login(client, path, token):
    if token:
        client.cookies.set("crucible_user_token", token)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("login")


def test_api_key_rotation_refuses_get(client):
    assert client.get("/regenerate_apikey", follow_redirects=False).status_code == 404
    assert client.get("/auth/apikey/regenerate", follow_redirects=False).status_code == 405
    assert client.post("/auth/apikey/regenerate", follow_redirects=False).status_code == 307


def test_session_token_round_trips_and_rejects_expired_forged_or_unbounded_tokens():
    import time

    claims = {"orcid": "0000-0001", "exp": int(time.time()) + 60}
    token = auth_utils.encode_token(claims, secret_key="k" * 32)
    assert auth_utils.decode_token(token, secret_key="k" * 32)["orcid"] == "0000-0001"
    assert auth_utils.decode_token(token, secret_key="x" * 32) is None
    expired = auth_utils.encode_token({**claims, "exp": int(time.time()) - 5}, secret_key="k" * 32)
    assert auth_utils.decode_token(expired, secret_key="k" * 32) is None
    unbounded = auth_utils.encode_token({"orcid": "0000-0001"}, secret_key="k" * 32)
    assert auth_utils.decode_token(unbounded, secret_key="k" * 32) is None
    assert auth_utils.decode_token("not-a-jwt", secret_key="k" * 32) is None


def test_callback_sets_a_session_cookie_that_decodes(client, monkeypatch):
    monkeypatch.setattr(oidc, "exchange_code", lambda *args, **kwargs: SIGNED_IN)
    state = start_login(client)
    response = client.get("/auth/callback", params={"code": "abc", "state": state}, follow_redirects=False)
    claims = auth_utils.decode_token(response.cookies["crucible_user_token"])
    assert claims["orcid"] == "0000-0001"
    assert claims["sub"] == "0000-0001"
    assert claims["given_name"] == "Ada"
    assert "family_name" not in claims
    assert "nonce" not in claims
