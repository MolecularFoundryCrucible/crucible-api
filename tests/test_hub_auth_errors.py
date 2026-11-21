from datetime import datetime, timedelta, timezone
import logging

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api import main
from app.api.dependencies import get_engine
from app.api.routes import browser_sign_in
from app.auth.login_transactions import SignInUnavailable, InvalidLoginRequest
from app.auth.tokens import TokenPair, InvalidGrant


MARKER = "sensitive-test-credential"
AUTH = ("crucible-hub", "test-client-secret-padded-to-32-chars00")


@pytest.fixture
def client(monkeypatch, caplog, orcid_provider_settings):
    caplog.set_level(logging.WARNING, logger="httpx")
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_ID", AUTH[0])
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", AUTH[1])
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "https://example.test/hub/auth/callback")
    orcid_provider_settings()
    monkeypatch.setitem(main.app.dependency_overrides, get_engine, lambda: object())
    with TestClient(main.app) as client:
        yield client


def assert_private(response):
    for name, value in main.SIGN_IN_HEADERS.items():
        assert response.headers[name] == value
    assert response.headers["X-Request-ID"]


@pytest.mark.parametrize("path,params", [
    ("authorize", {"state": MARKER, "code_challenge": MARKER, "max_age": MARKER}),
    ("callback/orcid", {"code": MARKER * 100, "state": MARKER}),
])
def test_query_validation_is_redacted(client, path, params, caplog):
    with caplog.at_level(logging.INFO):
        response = client.get("/oauth/" + path, params=params)
    assert response.status_code == 422
    assert MARKER not in response.text + caplog.text
    assert_private(response)


@pytest.mark.parametrize("prefix", ["", "/testapi", "/api/v3"])
def test_prefixes_routing_errors_and_redirects_are_private(client, monkeypatch, prefix):
    monkeypatch.setattr(main.app, "root_path", prefix)
    for path in {"/oauth", prefix + "/oauth"}:
        for method, suffix, status in [("GET", "/missing", 404), ("GET", "/token", 405), ("POST", "/token/", 307)]:
            response = client.request(method, path + suffix, follow_redirects=False)
            assert response.status_code == status
            assert_private(response)


def test_authentication_failures_are_private(client):
    response = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": "x" * 64})
    assert response.status_code in (400, 401)
    assert response.json()["error"] in ("invalid_client", "invalid_request")
    assert_private(response)


@pytest.mark.parametrize("error,status", [(InvalidGrant(), 400), (RuntimeError(MARKER), 500)])
def test_refresh_errors_do_not_disclose_internal_details(client, monkeypatch, caplog, error, status):
    def fail(*args, **kwargs):
        raise error

    from app.auth import oauth_server

    monkeypatch.setattr(oauth_server.RefreshGrant, "authenticate_refresh_token", lambda self, token: oauth_server._Credential(AUTH[0], 1))
    monkeypatch.setattr(oauth_server, "refresh_tokens", fail)
    with caplog.at_level(logging.INFO):
        response = client.post("/oauth/token", auth=AUTH, data={"grant_type": "refresh_token", "refresh_token": "x" * 64})
    assert response.status_code == status
    assert MARKER not in response.text + caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert_private(response)


def test_browser_failures_are_private(client, monkeypatch):
    def unavailable():
        raise SignInUnavailable

    def invalid(*args):
        raise InvalidLoginRequest

    monkeypatch.setattr(browser_sign_in, "load_login_config", unavailable)
    monkeypatch.setattr(browser_sign_in, "consume_login", invalid)
    response = client.get("/oauth/authorize", params={
        "client_id": "hub", "redirect_uri": "https://example.test/hub/auth/callback",
        "state": "s" * 16, "code_challenge": "c" * 43, "code_challenge_method": "S256", "response_type": "code",
    })
    assert response.status_code == 503
    assert_private(response)
    response = client.get("/oauth/callback/orcid", params={"code": MARKER, "state": MARKER})
    assert response.status_code == 400
    assert_private(response)


def test_successful_credentials_and_revocation_remain_private(client, monkeypatch):
    now = datetime.now(timezone.utc)
    pair = TokenPair("a" * 64, "r" * 64, now + timedelta(minutes=15), now + timedelta(days=30), "test-user")
    from app.auth import oauth_server

    monkeypatch.setattr(oauth_server.RefreshGrant, "authenticate_refresh_token", lambda self, token: oauth_server._Credential(AUTH[0], 1))
    monkeypatch.setattr(oauth_server, "refresh_tokens", lambda *args: pair)
    monkeypatch.setattr(oauth_server, "revoke_tokens", lambda *args: None)
    response = client.post("/oauth/token", auth=AUTH, data={"grant_type": "refresh_token", "refresh_token": "r" * 64})
    assert response.status_code == 200
    assert response.json()["access_token"] == pair.access_token
    assert_private(response)
    response = client.post("/oauth/revoke", auth=AUTH, data={"token": "r" * 64})
    assert response.status_code == 200
    assert_private(response)


def test_non_hub_errors_keep_framework_behavior():
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, main.handle_request_validation)
    app.middleware("http")(main.log_requests)

    @app.get("/oauth-other")
    def unrelated(number: int):
        return number

    @app.get("/unrelated-failure")
    def failure():
        raise RuntimeError("normal framework error")

    with TestClient(app) as client:
        response = client.get("/oauth-other", params={"number": "invalid"})
        assert response.status_code == 422
        assert response.json()["detail"][0]["loc"] == ["query", "number"]
        assert "cache-control" not in response.headers
        with pytest.raises(RuntimeError, match="normal framework error"):
            client.get("/unrelated-failure")


def test_openapi_documents_sign_in_error_headers():
    schema = main.app.openapi()
    for path, item in schema["paths"].items():
        if path.startswith("/oauth/callback/"):
            for operation in item.values():
                assert "500" in operation["responses"]
                for response in operation["responses"].values():
                    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
