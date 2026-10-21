import pytest
import requests

import app.api.routes.auth as auth_routes
import app.utils.auth_utils as auth_utils


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
