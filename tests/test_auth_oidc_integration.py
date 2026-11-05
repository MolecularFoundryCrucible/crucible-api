"""The Hub sign-in flow against real OIDC providers. Start them with the commands in
.github/workflows/tests.yml (Dex) and .github/workflows/oidc-authentik.yml (Authentik)."""
import base64
import hashlib
import json
import os
import re
from html import unescape
from urllib.parse import parse_qs, quote, urljoin, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, select

from app.api.dependencies import get_engine
from app.api.routes import hub_auth as routes
from app.config import get_settings
from app.models.core_models import HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, User, UserIdentity

API_URL = "http://localhost:7777"
HUB_CALLBACK = "http://localhost:3000/hub/auth/callback"
PASSWORD = "ci-only-password-not-real"
CLIENT_SECRET = "ci-only-client-secret"
VERIFIER = "v" * 43
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
LOGIN = {"client_id": "crucible-hub", "redirect_uri": HUB_CALLBACK, "state": "hub-state-123456789", "code_challenge": CHALLENGE, "code_challenge_method": "S256"}
DEX_ISSUER = os.getenv("OIDC_TEST_DEX_ISSUER")
SECOND_DEX_ISSUER = os.getenv("OIDC_TEST_DEX_SECOND_ISSUER")
AUTHENTIK_ISSUER = os.getenv("OIDC_TEST_AUTHENTIK_ISSUER")


def entry(key, issuer):
    return {
        "key": key, "display_name": f"{key} login", "issuer": issuer, "client_id": "crucible", "scopes": "openid profile email",
        "token_endpoint_auth_method": "client_secret_basic", "registration": "closed", "legacy_callback": False,
    }


def _at_callback(url):
    return url.startswith(f"{API_URL}/auth/hub/oidc/")


def dex_sign_in(browser, url, username):
    for _ in range(12):
        if _at_callback(url):
            return url
        response = browser.get(url)
        if response.is_redirect:
            url = urljoin(url, response.headers["location"])
            continue
        action = re.search(r'<form[^>]*action="([^"]*)"', response.text)
        assert action, response.text[:300]
        posted = browser.post(urljoin(url, unescape(action.group(1))), data={"login": f"{username}@example.org", "password": PASSWORD})
        assert posted.is_redirect, posted.text[:300]
        url = urljoin(url, posted.headers["location"])
    raise AssertionError(f"stuck at {url}")


def authentik_sign_in(browser, url, username):
    for _ in range(12):
        if _at_callback(url):
            return url
        response = browser.get(url)
        if response.is_redirect:
            url = urljoin(url, response.headers["location"])
            continue
        parsed = urlparse(url)
        assert parsed.path.startswith("/if/flow/"), url
        slug = parsed.path.rstrip("/").split("/")[-1]
        executor = f"{parsed.scheme}://{parsed.netloc}/api/v3/flows/executor/{slug}/?query={quote(parsed.query)}"
        step = browser.get(executor, follow_redirects=True).json()
        for _ in range(8):
            component = step["component"]
            headers = {"X-authentik-CSRF": browser.cookies.get("authentik_csrf", "")}
            if component == "ak-stage-identification":
                step = browser.post(executor, headers=headers, json={"component": component, "uid_field": username}, follow_redirects=True).json()
            elif component == "ak-stage-password":
                step = browser.post(executor, headers=headers, json={"component": component, "password": PASSWORD}, follow_redirects=True).json()
            elif component == "xak-flow-redirect":
                url = urljoin(url, step["to"])
                break
            else:
                raise AssertionError(f"stage {component}: {step}")
    raise AssertionError(f"stuck at {url}")


PROVIDERS = {
    "dex": (DEX_ISSUER, dex_sign_in),
    "authentik": (AUTHENTIK_ISSUER, authentik_sign_in),
}


@pytest.fixture(params=sorted(PROVIDERS))
def provider(request):
    issuer, sign_in = PROVIDERS[request.param]
    if not issuer:
        pytest.skip(f"requires a running {request.param}")
    return request.param, issuer, sign_in


@pytest.fixture
def seen(monkeypatch):
    identities = []
    real = routes.verified_identity

    async def recording(*args, **kwargs):
        identity = await real(*args, **kwargs)
        identities.append(identity)
        return identity

    monkeypatch.setattr(routes, "verified_identity", recording)
    return identities


@pytest.fixture
def api(db_engine, monkeypatch, seen):
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginCode, HubLoginTransaction, HubTokenFamily, HubRefreshToken, HubAccessToken)])
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_ID", "crucible-hub")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "hub-secret")
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", HUB_CALLBACK)
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", API_URL)
    for key in ("DEX", "SECOND", "AUTHENTIK"):
        monkeypatch.setenv(f"OIDC_{key}_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_engine] = lambda: db_engine
    with TestClient(app, base_url=API_URL) as client:
        yield client


def configure(monkeypatch, *entries):
    monkeypatch.setenv("CRUCIBLE_AUTH_PROVIDERS", json.dumps(list(entries)))
    get_settings.cache_clear()


def start(api, key, **extra):
    started = api.get("/auth/hub/login", params={**LOGIN, "provider": key, **extra}, follow_redirects=False)
    assert started.status_code == 302, started.text
    return started.headers["location"]


def sign_in(api, key, sign_in_at_provider, username="ada", **extra):
    with httpx.Client(timeout=30) as browser:
        callback = sign_in_at_provider(browser, start(api, key, **extra), username)
    assert callback.startswith(f"{API_URL}/auth/hub/oidc/{key}/callback?"), callback
    return api.get(callback.removeprefix(API_URL), follow_redirects=False)


def hub_answer(response):
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith(HUB_CALLBACK + "?")
    return {name: values[0] for name, values in parse_qs(urlparse(response.headers["location"]).query).items()}


def link(db_engine, issuer, subject, unique_id="0tmnyhfztdwc1000w2kspzsnh0"):
    with Session(db_engine) as session, session.begin():
        user = User(unique_id=unique_id, first_name="Ada", last_name="Lovelace")
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, issuer=issuer, subject=subject))
    return unique_id


def test_stranger_is_denied_after_a_real_sign_in(api, provider, monkeypatch, db_engine, seen):
    key, issuer, at_provider = provider
    configure(monkeypatch, entry(key, issuer))
    assert hub_answer(sign_in(api, key, at_provider)) == {"error": "access_denied", "state": LOGIN["state"]}
    assert seen[0].subject
    with Session(db_engine) as session:
        assert session.exec(select(User)).all() == []


def test_full_code_flow_and_the_same_subject_on_the_next_sign_in(api, provider, monkeypatch, db_engine, seen):
    key, issuer, at_provider = provider
    configure(monkeypatch, entry(key, issuer))
    sign_in(api, key, at_provider)
    unique_id = link(db_engine, issuer, seen[0].subject)
    answer = hub_answer(sign_in(api, key, at_provider))
    assert seen[1].subject == seen[0].subject
    tokens = api.post("/auth/hub/token", auth=("crucible-hub", "hub-secret"), json={"code": answer["code"], "code_verifier": VERIFIER, "redirect_uri": HUB_CALLBACK})
    assert tokens.status_code == 200, tokens.text
    assert tokens.json()["user_unique_id"] == unique_id
    refreshed = api.post("/auth/hub/refresh", auth=("crucible-hub", "hub-secret"), json={"refresh_token": tokens.json()["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text


def test_real_provider_uses_pkce(api, provider, monkeypatch):
    key, issuer, _ = provider
    configure(monkeypatch, entry(key, issuer))
    query = parse_qs(urlparse(start(api, key)).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == [f"{API_URL}/auth/hub/oidc/{key}/callback"]


def test_another_person_gets_login_required(api, provider, monkeypatch, db_engine, seen):
    key, issuer, at_provider = provider
    configure(monkeypatch, entry(key, issuer))
    sign_in(api, key, at_provider)
    unique_id = link(db_engine, issuer, seen[0].subject)
    assert "code" in hub_answer(sign_in(api, key, at_provider, required_user=unique_id))
    assert hub_answer(sign_in(api, key, at_provider, username="grace", required_user=unique_id))["error"] == "login_required"
    assert seen[-1].subject != seen[0].subject


def test_authentik_forced_sign_in_is_recent(api, monkeypatch, db_engine, seen):
    if not AUTHENTIK_ISSUER:
        pytest.skip("requires a running authentik")
    configure(monkeypatch, entry("authentik", AUTHENTIK_ISSUER))
    assert "prompt=login" in start(api, "authentik", max_age="0")
    sign_in(api, "authentik", authentik_sign_in, max_age="0")
    assert seen[0].auth_time is not None


@pytest.mark.skipif(not (DEX_ISSUER and SECOND_DEX_ISSUER), reason="requires two running dex servers")
def test_dex_identical_subject_at_a_second_issuer_stays_separate(api, monkeypatch, db_engine, seen):
    configure(monkeypatch, entry("dex", DEX_ISSUER), entry("second", SECOND_DEX_ISSUER))
    sign_in(api, "dex", dex_sign_in)
    link(db_engine, DEX_ISSUER, seen[0].subject)
    assert "code" in hub_answer(sign_in(api, "dex", dex_sign_in))
    assert hub_answer(sign_in(api, "second", dex_sign_in)) == {"error": "access_denied", "state": LOGIN["state"]}
    assert seen[-1].subject == seen[0].subject


@pytest.mark.skipif(not (DEX_ISSUER and SECOND_DEX_ISSUER), reason="requires two running dex servers")
def test_dex_code_at_the_other_providers_callback_is_refused(api, monkeypatch, seen):
    configure(monkeypatch, entry("dex", DEX_ISSUER), entry("second", SECOND_DEX_ISSUER))
    with httpx.Client(timeout=30) as browser:
        callback = dex_sign_in(browser, start(api, "dex"), "ada")
    refused = api.get(callback.removeprefix(API_URL).replace("/oidc/dex/", "/oidc/second/"), follow_redirects=False)
    assert hub_answer(refused)["error"] == "access_denied"
    assert seen == []


@pytest.mark.skipif(not (DEX_ISSUER and SECOND_DEX_ISSUER), reason="requires two running dex servers")
def test_dex_choice_page_leads_to_the_chosen_provider(api, monkeypatch):
    configure(monkeypatch, entry("dex", DEX_ISSUER), entry("second", SECOND_DEX_ISSUER))
    page = api.get("/auth/hub/login", params=LOGIN, follow_redirects=False)
    assert page.status_code == 200
    links = [unescape(link) for link in re.findall(r'href="(\?[^"]+)"', page.text)]
    assert len(links) == 2
    chosen = api.get(f"/auth/hub/login{links[1]}", follow_redirects=False)
    assert chosen.headers["location"].startswith(f"{SECOND_DEX_ISSUER}/auth?")
