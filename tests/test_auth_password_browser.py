"""Real browsers, headless: the password form's CSP (`form-action 'self'` plus the client's origin)
must let the redirect after the form post reach a client on another origin. Chrome and Firefox
apply form-action to that redirect, so only a browser can prove it. Skipped without Playwright's
browsers, unless CRUCIBLE_BROWSER_TESTS=1 (CI), which makes a missing browser a failure."""
import base64
import hashlib
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import pytest
from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app import config
from app.api.dependencies import get_engine
from app.auth.passwords import store_password
from app.models.core_models import (
    AuthEvent, DeviceAuthorization, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, LocalCredential, LoginThrottle, PasswordToken, User,
    UserIdentity,
)

REQUIRED = os.getenv("CRUCIBLE_BROWSER_TESTS") == "1"
RIGHT = "violet ladder under quiet rain"
VERIFIER = "k" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def servers(tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api") if not REQUIRED else __import__("playwright.sync_api", fromlist=["sync_playwright"])
    import uvicorn
    from fastapi import FastAPI

    from app.api.routes import hub_auth, oauth, passwords

    api_port, hub_port = free_port(), free_port()
    api_url, callback = f"http://127.0.0.1:{api_port}", f"http://127.0.0.1:{hub_port}/hub/auth/callback"
    for name, value in {
        "HUB_AUTH_ENABLED": "true", "HUB_AUTH_CLIENT_SECRET": "hub-secret-padded-to-at-least-32-chars00", "HUB_AUTH_CALLBACK_URI": callback,
        "HUB_AUTH_PUBLIC_API_URL": api_url, "CRUCIBLE_AUTH_PASSWORD_ENABLED": "true", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one",
        "CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("CRUCIBLE_AUTH_PROVIDERS", raising=False)
    monkeypatch.setattr(hub_auth, "get_api_root_path", lambda: "")
    monkeypatch.setattr(passwords, "get_api_root_path", lambda: "")
    config.get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{tmp_path / 'browser.db'}", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in (User, UserIdentity, AuthEvent, HubLoginTransaction, HubLoginCode, HubTokenFamily, HubRefreshToken, HubAccessToken, LocalCredential, LoginThrottle, PasswordToken, DeviceAuthorization)])
    with Session(engine) as session:
        user = User(unique_id="0tmnyhfztdwc1000w2kspzsnh0", username="ada.lovelace", first_name="Ada", last_name="Lovelace")
        session.add(user)
        session.commit()
        store_password(session, user, RIGHT)
        session.commit()

    app = FastAPI()
    for router in (hub_auth.router, hub_auth.password_router, oauth.router, passwords.pages):
        app.include_router(router)
    app.dependency_overrides[get_engine] = lambda: engine
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    api_thread = threading.Thread(target=server.run, daemon=True)
    api_thread.start()

    arrived = []

    class Hub(BaseHTTPRequestHandler):
        def do_GET(self):
            arrived.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"signed in")

        def log_message(self, *args):
            pass

    hub = HTTPServer(("127.0.0.1", hub_port), Hub)
    threading.Thread(target=hub.serve_forever, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield sync_api, api_url, callback, arrived, engine
    server.should_exit = True
    hub.shutdown()
    api_thread.join(timeout=5)
    engine.dispose()


@pytest.mark.parametrize("browser_name", ["chromium", "firefox"])
def test_a_real_browser_reaches_a_client_on_another_origin_after_the_form(servers, browser_name):
    sync_api, api_url, callback, arrived, _ = servers
    params = {"response_type": "code", "client_id": "crucible-hub", "redirect_uri": callback, "state": "client-state-123456789", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
    with sync_api.sync_playwright() as playwright:
        try:
            browser = getattr(playwright, browser_name).launch(headless=True)
        except Exception as error:
            if REQUIRED:
                raise
            pytest.skip(f"{browser_name} is not installed for Playwright: {type(error).__name__}")
        try:
            page = browser.new_page()
            violations = []
            page.on("console", lambda message: violations.append(message.text) if "form-action" in message.text else None)
            page.goto(f"{api_url}/oauth/authorize?{urlencode(params)}")
            assert urlparse(page.url).path == "/oauth/password"
            page.fill('input[name="username"]', "ada.lovelace")
            page.fill('input[name="password"]', RIGHT)
            page.check("#show-password")
            assert page.get_attribute("#password", "type") == "text"
            page.click('button[type="submit"]')
            for _ in range(100):
                if arrived:
                    break
                time.sleep(0.05)
        finally:
            browser.close()
    assert arrived, f"the redirect to the client never arrived; CSP messages: {violations}"
    answer = parse_qs(urlparse(arrived[0]).query)
    assert answer["state"] == ["client-state-123456789"] and answer["iss"] == [api_url] and answer["code"]
    assert not violations


@pytest.mark.parametrize("browser_name", ["chromium", "firefox"])
def test_a_real_browser_sets_a_password_through_a_link(servers, browser_name):
    from app.auth.password_links import issue_link
    from app.auth.throttle import attempt_password

    sync_api, api_url, _, _, engine = servers
    with Session(engine) as session, session.begin():
        user = session.get(User, 1)
        token, _ = issue_link(session, user, "reset", actor=None)
    new = "a brand new password for the browser"
    with sync_api.sync_playwright() as playwright:
        try:
            browser = getattr(playwright, browser_name).launch(headless=True)
        except Exception as error:
            if REQUIRED:
                raise
            pytest.skip(f"{browser_name} is not installed for Playwright: {type(error).__name__}")
        try:
            page = browser.new_page()
            violations = []
            page.on("console", lambda message: violations.append(message.text) if "Content Security Policy" in message.text or "CSP" in message.text else None)
            page.goto(f"{api_url}/oauth/password-link?token={token}")
            assert page.url == f"{api_url}/oauth/password-link"
            page.fill('input[name="password"]', new)
            page.fill('input[name="confirm"]', new)
            page.check("#show-password")
            assert page.get_attribute("#confirm", "type") == "text"
            page.click('button[type="submit"]')
            page.wait_for_selector("text=Password changed")
        finally:
            browser.close()
    assert not violations
    assert attempt_password(engine, 1, new, None)
