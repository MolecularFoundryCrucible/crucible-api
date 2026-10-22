import pytest
import requests

import app.api.routes.auth as auth_routes
import app.utils.auth_utils as auth_utils


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
    monkeypatch.setattr(auth_utils, "code_for_token_exchange", lambda *args: "token")
    monkeypatch.setattr(auth_routes.requests, "get", lambda *args, **kwargs: type("R", (), {"json": lambda self: {"sub": "0000-0001"}})())
    if cookie is not None:
        client.cookies.set("after_login_return_to_page", cookie)
    response = client.get("/auth/callback", params={"code": "abc"}, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == target


def test_orcid_failure_is_502(client, monkeypatch):
    def fail(*args, **kwargs):
        raise requests.Timeout("slow")
    monkeypatch.setattr(auth_utils, "code_for_token_exchange", fail)
    assert client.get("/auth/callback", params={"code": "abc"}, follow_redirects=False).status_code == 502


@pytest.mark.parametrize("path", ["/user_apikey", "/auth/apikey", "/regenerate_apikey"])
@pytest.mark.parametrize("token", [None, "expired-or-forged"])
def test_missing_or_invalid_session_redirects_to_login(client, path, token):
    if token:
        client.cookies.set("crucible_user_token", token)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("login")


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
    monkeypatch.setattr(auth_utils, "code_for_token_exchange", lambda *args: "token")
    monkeypatch.setattr(auth_routes.requests, "get", lambda *args, **kwargs: type("R", (), {"json": lambda self: {"sub": "0000-0001"}})())
    response = client.get("/auth/callback", params={"code": "abc"}, follow_redirects=False)
    assert auth_utils.decode_token(response.cookies["crucible_user_token"])["orcid"] == "0000-0001"
