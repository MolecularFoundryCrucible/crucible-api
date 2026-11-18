import base64
import hashlib
import logging
import re
import statistics
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app import config
from app.api.dependencies import get_engine
from app.api.routes import browser_sign_in as routes
from app.api.routes import oauth as oauth_routes
from app.auth.passwords import store_password
from app.tables import (
    AuthEvent, AccessToken, LoginCode, LoginTransaction, RefreshToken, TokenFamily, LocalCredential, LoginThrottle, User,
)

API_URL = "http://localhost:7777"
CALLBACK = "https://hub.example/hub/auth/callback"
SECRET = "hub-secret-padded-to-at-least-32-chars00"
VERIFIER = "k" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
RIGHT = "violet ladder under quiet rain"
MARKER = "marker-password-value-1234567"
FAILED = "Sign-in failed. Check your details, or try again in a few minutes."


@pytest.fixture
def settings(monkeypatch):
    values = {
        "HUB_AUTH_ENABLED": "true", "HUB_AUTH_CLIENT_SECRET": SECRET, "HUB_AUTH_CALLBACK_URI": CALLBACK, "HUB_AUTH_PUBLIC_API_URL": API_URL,
        "CRUCIBLE_AUTH_PASSWORD_ENABLED": "true", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one",
        "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CRUCIBLE_AUTH_PROVIDERS", raising=False)
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    config.get_settings.cache_clear()


@pytest.fixture
def client(db_engine, settings):
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (LoginTransaction, LoginCode, TokenFamily, RefreshToken, AccessToken, LocalCredential, LoginThrottle)])
    from fastapi.exceptions import RequestValidationError

    from app.api import main

    app = FastAPI()
    app.add_exception_handler(RequestValidationError, main.handle_request_validation)
    for router in (routes.router, routes.password_router, oauth_routes.router):
        app.include_router(router)
    app.dependency_overrides[get_engine] = lambda: db_engine
    with TestClient(app, base_url=API_URL) as test_client:
        yield test_client


def person(engine, username="ada.lovelace", *, password=RIGHT, **fields):
    with Session(engine) as session:
        user = User(unique_id=f"0tmnyhfztdwc1000w2ksp{len(username):02d}{username[:3]}", username=username, first_name="Ada", last_name="Lovelace", **fields)
        session.add(user)
        session.commit()
        if password is not None:
            store_password(session, user, password)
            session.commit()
        return user.id


def start(client, **extra):
    params = {"response_type": "code", "client_id": "crucible-hub", "redirect_uri": CALLBACK, "state": "client-state-123456789", "code_challenge": CHALLENGE, "code_challenge_method": "S256", **extra}
    started = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert started.status_code == 302, started.text
    location = urlparse(started.headers["location"])
    assert location.path == "/oauth/password"
    return parse_qs(location.query)["state"][0]


def submit(client, state, username="ada.lovelace", password=RIGHT):
    return client.post("/oauth/password", data={"username": username, "password": password, "state": state}, follow_redirects=False)


def answer(response):
    assert response.status_code == 303, response.text
    assert response.headers["location"].startswith(CALLBACK + "?")
    return {name: values[0] for name, values in parse_qs(urlparse(response.headers["location"]).query).items()}


def test_password_sign_in_returns_a_code_that_gives_a_recent_session(client, db_engine):
    person(db_engine)
    state = start(client)
    form = client.get("/oauth/password", params={"state": state})
    assert form.status_code == 200 and 'autocomplete="current-password"' in form.text and 'autocomplete="username"' in form.text
    result = answer(submit(client, state))
    assert (result["state"], result["iss"]) == ("client-state-123456789", API_URL)
    tokens = client.post("/oauth/token", auth=("crucible-hub", SECRET), data={"grant_type": "authorization_code", "code": result["code"], "code_verifier": VERIFIER, "redirect_uri": CALLBACK})
    assert tokens.status_code == 200, tokens.text
    assert abs(tokens.json()["auth_time"] - datetime.now(timezone.utc).timestamp()) < 30
    with Session(db_engine) as session:
        event = session.exec(select(AuthEvent).where(AuthEvent.event == "sign_in")).one()
    assert (event.issuer, event.client_id) == (API_URL, "crucible-hub")


def test_a_password_only_deployment_goes_straight_to_the_form(client):
    assert start(client)


def test_the_password_form_comes_first_and_offers_the_providers_below(client, db_engine, orcid_provider_settings):
    orcid_provider_settings()
    person(db_engine)
    started = client.get("/oauth/authorize", params={"response_type": "code", "client_id": "crucible-hub", "redirect_uri": CALLBACK, "state": "client-state-123456789", "code_challenge": CHALLENGE, "code_challenge_method": "S256", "max_age": 0}, follow_redirects=False)
    assert started.status_code == 302, started.text
    location = urlparse(started.headers["location"])
    assert location.path == "/oauth/password" and parse_qs(location.query)["max_age"] == ["0"]
    state = parse_qs(location.query)["state"][0]
    for page in (client.get("/oauth/password", params={"state": state, "max_age": 0}), client.post("/oauth/password", data={"username": "ada.lovelace", "password": "not the right one, but long", "state": state, "max_age": 0})):
        assert 'autocomplete="current-password"' in page.text and "Sign in with ORCID" in page.text and 'name="max_age" value="0"' in page.text
        link = urlparse(re.search(r'href="([^"]+provider=orcid[^"]*)"', page.text).group(1).replace("&amp;", "&"))
        assert f"{link.scheme}://{link.netloc}{link.path}" == f"{API_URL}/oauth/authorize"
        assert {name: values[0] for name, values in parse_qs(link.query).items()} == {
            "response_type": "code", "client_id": "crucible-hub", "redirect_uri": CALLBACK, "state": "client-state-123456789",
            "code_challenge": CHALLENGE, "code_challenge_method": "S256", "max_age": "0", "provider": "orcid",
        }
    assert "Sign in with ORCID" not in client.get("/oauth/password", params={"state": "unknown"}).text


def test_a_named_provider_still_skips_the_password_form(client, orcid_provider_settings, monkeypatch):
    orcid_provider_settings()

    async def unreachable(*args, **kwargs):
        raise routes.ProviderUnavailable
    monkeypatch.setattr(routes, "new_code_verifier", unreachable)
    named = client.get("/oauth/authorize", params={"response_type": "code", "client_id": "crucible-hub", "redirect_uri": CALLBACK, "state": "client-state-123456789", "code_challenge": CHALLENGE, "code_challenge_method": "S256", "provider": "orcid"}, follow_redirects=False)
    assert named.status_code == 302 and "/oauth/password" not in named.headers["location"]


@pytest.mark.parametrize("case", ["unknown user", "wrong password", "delayed", "locked", "disabled", "no password set", "service account"])
def test_all_failures_give_identical_body_and_status(client, db_engine, case):
    username, password = "ada.lovelace", RIGHT
    if case == "unknown user":
        username = "nobody.here"
    elif case == "wrong password":
        person(db_engine)
        password = "not the right one, but long"
    elif case in ("delayed", "locked"):
        user = person(db_engine)
        with Session(db_engine) as session:
            row = session.get(LocalCredential, user)
            row.blocked_until = datetime.now(timezone.utc) + timedelta(minutes=5)
            row.locked_at = datetime.now(timezone.utc) if case == "locked" else None
            session.add(row)
            session.commit()
    elif case == "disabled":
        person(db_engine, disabled_at=datetime.now(timezone.utc))
    elif case == "no password set":
        person(db_engine, password=None)
    elif case == "service account":
        with Session(db_engine) as session:
            session.add(User(unique_id="0tmnyhfztdwc1000w2kspzsnh7", username="ada.lovelace", first_name="R", last_name="B", is_service_account=True))
            session.commit()
    response = submit(client, start(client), username, password)
    assert response.status_code == 401 and FAILED in response.text
    body = re.sub(r'nonce="[^"]+"|name="state" value="[^"]+"', "", response.text)
    reference = re.sub(r'nonce="[^"]+"|name="state" value="[^"]+"', "", submit(client, start(client), username, "another wrong password here").text)
    assert body == reference


def test_timing_comparable_across_failures(client, db_engine):
    person(db_engine)

    def median(username, password):
        runs = []
        for _ in range(5):
            state = start(client)
            began = time.perf_counter()
            submit(client, state, username, password)
            runs.append(time.perf_counter() - began)
        return statistics.median(runs)

    wrong = median("ada.lovelace", "not the right one, but long")
    unknown = median("nobody.here", "not the right one, but long")
    assert 0.4 < unknown / wrong < 2.5


def test_marker_password_in_no_log_or_response(client, db_engine, caplog):
    person(db_engine)
    with caplog.at_level(logging.DEBUG):
        state = start(client)
        responses = [submit(client, state, "ada.lovelace", MARKER), submit(client, state, "nobody", MARKER)]
        responses.append(client.post("/oauth/password", data={"username": "ada.lovelace", "password": MARKER * 40, "state": state}))
        responses.append(client.post("/oauth/password", data={"password": MARKER}))
    assert all(MARKER not in response.text for response in responses)
    assert MARKER not in caplog.text


def test_typo_keeps_the_sign_in_and_success_uses_it_up(client, db_engine):
    person(db_engine)
    state = start(client)
    assert submit(client, state, password="a typo in the passphrase").status_code == 401
    answer(submit(client, state))
    assert submit(client, state).status_code == 400


def test_form_past_the_sign_in_lifetime_says_expired_and_is_not_counted(client, db_engine):
    user = person(db_engine)
    state = start(client)
    with Session(db_engine) as session:
        for transaction in session.exec(select(LoginTransaction)).all():
            transaction.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.add(transaction)
        session.commit()
    response = submit(client, state, password="wrong but not counted here")
    assert response.status_code == 400 and "expired" in response.text
    with Session(db_engine) as session:
        assert session.get(LocalCredential, user).failed_attempts == 0


def test_post_without_the_browser_cookie_is_refused(client, db_engine):
    user = person(db_engine)
    state = start(client)
    client.cookies.clear()
    assert submit(client, state).status_code == 400
    with Session(db_engine) as session:
        assert session.get(LocalCredential, user).failed_attempts == 0


def test_get_never_authenticates(client, db_engine):
    person(db_engine)
    state = start(client)
    page = client.get("/oauth/password", params={"state": state, "username": "ada.lovelace", "password": RIGHT}, follow_redirects=False)
    assert page.status_code == 200 and "location" not in page.headers


def test_uppercase_and_spaced_username_finds_account_and_password_never_altered(client, db_engine):
    person(db_engine)
    answer(submit(client, start(client), "  ADA.Lovelace "))
    assert submit(client, start(client), "ada.lovelace", RIGHT + " ").status_code == 401
    assert submit(client, start(client), "ada.lovelace", RIGHT.upper()).status_code == 401


def test_routes_are_404_when_passwords_are_off(client, monkeypatch):
    state = start(client)
    monkeypatch.setenv("CRUCIBLE_AUTH_PASSWORD_ENABLED", "false")
    config.get_settings.cache_clear()
    assert client.get("/oauth/password", params={"state": state}).status_code == 404
    assert submit(client, state).status_code == 404


def test_step_up_with_max_age_shows_the_form_again(client, db_engine):
    person(db_engine)
    answer(submit(client, start(client)))
    state = start(client, max_age="0")
    assert client.get("/oauth/password", params={"state": state}).status_code == 200


def test_device_cookie_set_on_success_and_used_next_time(client, db_engine):
    person(db_engine)
    response = submit(client, start(client))
    answer(response)
    cookie = [header for header in response.headers.get_list("set-cookie") if header.startswith("crucible_device=")]
    assert cookie and "HttpOnly" in cookie[0] and "Path=/oauth/password" in cookie[0]


def test_form_allows_paste_and_its_script_runs_under_the_csp(client):
    state = start(client)
    form = client.get("/oauth/password", params={"state": state})
    csp = form.headers["content-security-policy"]
    script_nonce = re.search(r"script-src 'nonce-([^']+)'", csp).group(1)
    assert f'<script nonce="{script_nonce}">' in form.text
    assert "onpaste" not in form.text and "unsafe-inline" not in csp
    assert "form-action 'self' https://hub.example" in csp and "frame-ancestors 'none'" in csp
    assert form.headers["cache-control"] == "no-store" and form.headers["referrer-policy"] == "no-referrer"


def test_waiting_account_sees_the_waiting_page_only_with_the_right_password(client, db_engine):
    person(db_engine, pending_at=datetime.now(timezone.utc))
    assert submit(client, start(client), password="not the right one, but long").status_code == 401
    waiting = submit(client, start(client))
    assert waiting.status_code == 403 and "location" not in waiting.headers


def test_required_user_mismatch_gets_login_required(client, db_engine):
    person(db_engine)
    result = answer(submit(client, start(client, required_user="0tmnyhfztdwc1000w2kspzsnh9")))
    assert result["error"] == "login_required" and "code" not in result


def test_passwords_with_plain_http_public_address_refuse_startup(settings, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "http://crucible.example.org")
    config.get_settings.cache_clear()
    with pytest.raises(config.ConfigurationError, match="https"):
        config.check_settings()
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "http://localhost:8080")
    config.get_settings.cache_clear()
    config.check_settings()


def test_clearing_the_username_of_a_password_account_is_refused(db_engine, client, auth_as):
    from app.api.main import app as main_app

    person(db_engine)
    with Session(db_engine) as session:
        unique_id = session.exec(select(User.unique_id)).one()
    auth_as(unique_id)
    with TestClient(main_app) as main_client:
        refused = main_client.patch("/account/profile", json={"username": None})
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "username_required")


def test_a_public_client_gets_the_code_only_after_the_person_allows_it(client, db_engine, monkeypatch):
    import html
    import json

    loopback = "http://127.0.0.1:53817/callback"
    for name in ("HUB_AUTH_ENABLED", "HUB_AUTH_CLIENT_SECRET", "HUB_AUTH_CALLBACK_URI"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps([
        {"key": "hub", "client_id": "crucible-hub", "display_name": "Crucible Hub", "type": "confidential", "redirect_uris": [CALLBACK]},
        {"key": "nanocrucible", "client_id": "nano-crucible", "display_name": "nano-crucible", "type": "public", "redirect_uris": ["http://127.0.0.1/callback"]},
    ]))
    monkeypatch.setenv("OAUTH_HUB_CLIENT_SECRET", SECRET)
    config.get_settings.cache_clear()
    person(db_engine)
    state = start(client, client_id="nano-crucible", redirect_uri=loopback)
    page = submit(client, state)
    assert page.status_code == 200 and "location" not in page.headers
    assert "nano-crucible" in page.text and "127.0.0.1:53817" in page.text and "frame-ancestors 'none'" in page.headers["content-security-policy"]
    links = [html.unescape(href) for href in re.findall(r'href="([^"]+)"', page.text)]
    allow = next(link for link in links if "code=" in link)
    deny = next(link for link in links if "error=access_denied" in link)
    assert allow.startswith(loopback + "?") and deny.startswith(loopback + "?")
    code = parse_qs(urlparse(allow).query)["code"][0]
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "nano-crucible", "code": code, "code_verifier": VERIFIER, "redirect_uri": loopback})
    assert tokens.status_code == 200, tokens.text
    hub_state = start(client)
    answer(submit(client, hub_state))


def test_a_sign_in_request_without_a_redirect_is_refused_not_redirected(client):
    params = {"response_type": "code", "client_id": "crucible-hub", "state": "client-state-123456789", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    answer = client.get("/oauth/authorize", params=params, follow_redirects=False)
    assert answer.status_code == 400 and "location" not in answer.headers


def test_a_public_client_can_end_on_the_code_page_and_redeem_the_pasted_code(client, db_engine, monkeypatch):
    import html
    import json

    code_page = f"{API_URL}/oauth/code"
    for name in ("HUB_AUTH_ENABLED", "HUB_AUTH_CLIENT_SECRET", "HUB_AUTH_CALLBACK_URI"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("CRUCIBLE_AUTH_CLIENTS", json.dumps([
        {"key": "hub", "client_id": "crucible-hub", "display_name": "Crucible Hub", "type": "confidential", "redirect_uris": [CALLBACK]},
        {"key": "nanocrucible", "client_id": "nano-crucible", "display_name": "nano-crucible", "type": "public", "redirect_uris": ["http://127.0.0.1/callback"]},
    ]))
    monkeypatch.setenv("OAUTH_HUB_CLIENT_SECRET", SECRET)
    config.get_settings.cache_clear()
    person(db_engine)
    refused = client.get("/oauth/authorize", params={"response_type": "code", "client_id": "crucible-hub", "redirect_uri": code_page, "state": "client-state-123456789", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}, follow_redirects=False)
    assert refused.status_code == 400
    state = start(client, client_id="nano-crucible", redirect_uri=code_page)
    confirm = submit(client, state)
    assert "a code shown on this site" in confirm.text
    allow = next(html.unescape(href) for href in re.findall(r'href="([^"]+)"', confirm.text) if "code=" in html.unescape(href))
    assert allow.startswith(code_page + "?")
    shown = client.get(allow.removeprefix(API_URL))
    code = parse_qs(urlparse(allow).query)["code"][0]
    assert shown.status_code == 200 and code in shown.text and "Your sign-in code" in shown.text
    assert "form-action 'none'" in shown.headers["content-security-policy"] and shown.headers["cache-control"] == "no-store" and shown.headers["referrer-policy"] == "no-referrer"
    tokens = client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "nano-crucible", "code": code, "code_verifier": VERIFIER, "redirect_uri": code_page})
    assert tokens.status_code == 200, tokens.text
    assert client.post("/oauth/token", data={"grant_type": "authorization_code", "client_id": "nano-crucible", "code": code, "code_verifier": "x" * 43, "redirect_uri": code_page}).status_code == 400
    cancelled = client.get("/oauth/code", params={"error": "access_denied"})
    assert cancelled.status_code == 200 and "Sign-in not completed" in cancelled.text
