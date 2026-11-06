import base64
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import RSAKey
from sqlmodel import Session, SQLModel, select

from app.api.dependencies import get_engine
from app.api.routes import auth as auth_routes
from app.api.routes import hub_auth as routes
from app.config import get_settings, missing_required_settings
from app.models.core_models import HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, User, UserIdentity
from tests.test_auth_providers import LAB_ENTRY, ORCID_ENTRY, FakeProvider

HUB_CALLBACK = "https://hub.example/hub/auth/callback"
API_URL = "http://localhost:7777"
ORCID = "0000-0002-1825-0097"
LAB_ISSUER = LAB_ENTRY["issuer"]
LOGIN = {
    "client_id": "crucible-hub",
    "redirect_uri": HUB_CALLBACK,
    "state": "hub-state-123456789",
    "code_challenge": "a" * 43,
    "code_challenge_method": "S256",
}


@pytest.fixture
def remote(monkeypatch):
    fakes = {"orcid.org": FakeProvider(), "login.example.org": FakeProvider(LAB_ISSUER, "crucible")}

    async def handle(self, request):
        await request.aread()
        return fakes[request.url.host].respond(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    return fakes


@pytest.fixture
def configure(monkeypatch):
    def apply(*entries):
        monkeypatch.setenv("CRUCIBLE_AUTH_PROVIDERS", json.dumps(list(entries)))
        get_settings.cache_clear()
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_ID", "crucible-hub")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "hub-secret-padded-to-at-least-32-chars00")
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", HUB_CALLBACK)
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", API_URL)
    monkeypatch.setenv("OIDC_ORCID_CLIENT_SECRET", "orcid-secret")
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET", "lab-secret")
    monkeypatch.setattr(routes, "get_api_root_path", lambda: "")
    apply(ORCID_ENTRY, LAB_ENTRY)
    return apply


@pytest.fixture
def client(db_engine, configure, remote):
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginCode, HubLoginTransaction, HubTokenFamily, HubRefreshToken, HubAccessToken)])
    app = FastAPI()
    app.include_router(routes.router)
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_engine] = lambda: db_engine
    with TestClient(app, base_url=API_URL) as test_client:
        yield test_client


def start(client, remote, key=None, **extra):
    params = {**LOGIN, **extra, **({"provider": key} if key else {})}
    started = client.get("/auth/hub/login", params=params, follow_redirects=False)
    assert started.status_code == 302, started.text
    query = parse_qs(urlparse(started.headers["location"]).query)
    return query


def finish(client, path, query, code="provider-code"):
    return client.get(path, params={"code": code, "state": query["state"][0]}, follow_redirects=False)


def sign_in(client, remote, host, key, path, subject, **extra):
    query = start(client, remote, key, **extra)
    remote[host].claims = {"nonce": query["nonce"][0], "sub": subject}
    return finish(client, path, query)


def lab_sign_in(client, remote, subject="lab-user", **extra):
    return sign_in(client, remote, "login.example.org", "lab", "/auth/hub/oidc/lab/callback", subject, **extra)


def orcid_sign_in(client, remote, subject=ORCID, **extra):
    return sign_in(client, remote, "orcid.org", "orcid", "/auth/hub/callback", subject, **extra)


def link(db_engine, issuer, subject, unique_id="0tmnyhfztdwc1000w2kspzsnh0", **fields):
    with Session(db_engine) as session, session.begin():
        user = User(unique_id=unique_id, first_name="Ada", last_name="Lovelace", **fields)
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, issuer=issuer, subject=subject))
    return unique_id


def hub_answer(response):
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith(HUB_CALLBACK + "?")
    answer = {name: values[0] for name, values in parse_qs(urlparse(response.headers["location"]).query).items()}
    assert answer.pop("iss") == API_URL
    return answer


def issued_codes(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(HubLoginCode)).all()


def people(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(User.unique_id)).all()


def test_methods_lists_each_provider_without_its_settings(client):
    assert client.get("/auth/methods").json() == [
        {"key": "orcid", "display_name": "ORCID", "kind": "oidc", "is_orcid": True},
        {"key": "lab", "display_name": "Example Lab", "kind": "oidc", "is_orcid": False},
    ]


def test_methods_is_empty_without_the_hub(client, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_ENABLED", "false")
    get_settings.cache_clear()
    assert client.get("/auth/methods").json() == []


def test_one_method_redirects_directly(client, remote, configure):
    configure(ORCID_ENTRY)
    assert start(client, remote)["client_id"] == ["APP-TEST"]


def test_several_methods_show_chooser_and_start_nothing(client, db_engine):
    page = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "ORCID" in page.text and "Example Lab" in page.text
    assert "crucible_hub_login" not in page.cookies
    with Session(db_engine) as session:
        assert session.exec(select(HubLoginTransaction)).all() == []


def test_chooser_refuses_a_bad_request_before_showing_anything(client):
    assert client.get("/auth/hub/login", params={**LOGIN, "client_id": "other"}, follow_redirects=False).status_code == 400
    assert client.get("/auth/hub/login", params={**LOGIN, "redirect_uri": "https://evil.example/cb"}, follow_redirects=False).status_code == 400


def test_chooser_cannot_be_framed_cached_or_leak_its_address(client):
    page = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False)
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["cache-control"] == "no-store"
    assert page.headers["referrer-policy"] == "no-referrer"
    assert page.headers["x-content-type-options"] == "nosniff"


def test_chooser_allows_no_script_and_only_its_own_style(client):
    first = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False)
    second = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False)
    policy = first.headers["content-security-policy"]
    assert "default-src 'none'" in policy and "base-uri 'none'" in policy and "form-action 'none'" in policy
    assert "unsafe" not in policy
    nonce = re.search(r"style-src 'nonce-([^']+)'", policy).group(1)
    assert f'<style nonce="{nonce}">' in first.text
    assert nonce not in second.headers["content-security-policy"]


def test_chooser_page_has_no_script_form_or_outside_address(client):
    text = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False).text.lower()
    assert "<script" not in text and "<form" not in text and "<input" not in text
    assert "http://" not in text.replace(HUB_CALLBACK.lower(), "") and "//" not in re.sub(r'href="\?[^"]+"', "", text)


def test_chooser_page_uses_no_blocked_markup(client):
    text = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False).text.lower()
    for blocked in ("style=", " src=", "<image", "<use", "<link", "url(", "@import", "@font-face", " on"):
        assert blocked not in re.sub(r'href="\?[^"]+"', "", text.replace("<a href", "<a-href")), blocked


def test_chooser_shows_the_logo_and_only_the_orcid_provider_gets_its_mark(client, configure):
    page = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False).text
    assert page.count('class="logo"') == 1
    assert page.count('class="provider-mark"') == 1
    assert page.index('class="provider-mark"') < page.index("Sign in with ORCID") < page.index("Sign in with Example Lab")
    configure({**LAB_ENTRY, "key": "one"}, {**LAB_ENTRY, "key": "two", "issuer": "https://other.example.org"})
    assert 'class="provider-mark"' not in client.get("/auth/hub/login", params=LOGIN, follow_redirects=False).text


def test_chooser_links_carry_only_known_parameters(client):
    page = client.get("/auth/hub/login", params={**LOGIN, "injected": "value", "next": "https://evil.example"}, follow_redirects=False)
    assert "injected" not in page.text and "evil.example" not in page.text


@pytest.mark.parametrize("field, hostile", [
    ("state", '"><script>alert(1)</script>aaaaaaaa'),
    ("required_user", '"><script>alert(1)</script>'),
])
def test_chooser_escapes_what_the_request_carries(client, field, hostile):
    page = client.get("/auth/hub/login", params={**LOGIN, field: hostile}, follow_redirects=False)
    assert page.status_code == 200
    assert "<script" not in page.text
    assert [link[field] for link in chooser_links(client, **{field: hostile})] == [[hostile], [hostile]]


def test_chooser_escapes_display_name(client, configure):
    configure(ORCID_ENTRY, {**LAB_ENTRY, "display_name": '<script>alert("x")</script>'})
    page = client.get("/auth/hub/login", params=LOGIN, follow_redirects=False)
    assert "<script>" not in page.text
    assert "&lt;script&gt;" in page.text


def chooser_links(client, **extra):
    page = client.get("/auth/hub/login", params={**LOGIN, **extra}, follow_redirects=False)
    return [parse_qs(unescape(link).lstrip("?")) for link in re.findall(r'href="(\?[^"]+)"', page.text)]


def test_chooser_links_keep_the_hubs_request(client):
    links = chooser_links(client, max_age="0", required_user="some-user")
    assert [link["provider"] for link in links] == [["orcid"], ["lab"]]
    for link in links:
        assert link["state"] == [LOGIN["state"]]
        assert link["code_challenge"] == [LOGIN["code_challenge"]]
        assert link["max_age"] == ["0"]
        assert link["required_user"] == ["some-user"]


def test_chooser_gives_each_method_its_own_state(client, remote):
    orcid = start(client, remote, "orcid")
    lab = start(client, remote, "lab")
    assert orcid["state"] != lab["state"]
    assert orcid["nonce"] != lab["nonce"]


def test_chooser_with_max_age_zero_forces_fresh_sign_in_for_each_method(client, remote):
    for key in ("orcid", "lab"):
        query = start(client, remote, key, max_age="0")
        assert query["max_age"] == ["0"] and query["prompt"] == ["login"]


def test_unknown_provider_is_refused(client):
    assert client.get("/auth/hub/login", params={**LOGIN, "provider": "nope"}, follow_redirects=False).status_code == 400


def test_each_provider_gets_its_own_callback_address(client, remote):
    assert start(client, remote, "orcid")["redirect_uri"] == [f"{API_URL}/auth/hub/callback"]
    assert start(client, remote, "lab")["redirect_uri"] == [f"{API_URL}/auth/hub/oidc/lab/callback"]


def test_callback_for_other_provider_refused(client, remote, db_engine):
    query = start(client, remote, "orcid")
    remote["login.example.org"].claims = {"nonce": query["nonce"][0]}
    answer = hub_answer(finish(client, "/auth/hub/oidc/lab/callback", query))
    assert answer == {"error": "access_denied", "state": LOGIN["state"]}
    assert remote["login.example.org"].token_requests == []
    assert remote["orcid.org"].token_requests == []
    assert issued_codes(db_engine) == []


def test_legacy_callback_only_serves_the_provider_marked_for_it(client, remote, db_engine):
    link(db_engine, LAB_ISSUER, "lab-user")
    query = start(client, remote, "lab")
    answer = hub_answer(finish(client, "/auth/hub/callback", query))
    assert answer["error"] == "access_denied"
    assert issued_codes(db_engine) == []


def test_legacy_provider_is_not_served_on_a_new_address(client, remote):
    query = start(client, remote, "orcid")
    assert hub_answer(finish(client, "/auth/hub/oidc/orcid/callback", query))["error"] == "access_denied"
    assert remote["orcid.org"].token_requests == []


def test_callback_for_unknown_provider_refused(client, remote):
    query = start(client, remote, "lab")
    assert hub_answer(finish(client, "/auth/hub/oidc/nope/callback", query))["error"] == "access_denied"


def test_state_from_other_browser_refused(client, remote):
    query = start(client, remote, "lab")
    client.cookies.clear()
    refused = finish(client, "/auth/hub/oidc/lab/callback", query)
    assert refused.status_code == 400
    assert "location" not in refused.headers
    assert remote["login.example.org"].token_requests == []


def test_pkce_sent_when_advertised(client, remote, db_engine):
    link(db_engine, LAB_ISSUER, "lab-user")
    remote["login.example.org"].metadata["code_challenge_methods_supported"] = ["plain", "S256"]
    query = start(client, remote, "lab")
    assert query["code_challenge_method"] == ["S256"]
    remote["login.example.org"].claims = {"nonce": query["nonce"][0], "sub": "lab-user"}
    assert "code" in hub_answer(finish(client, "/auth/hub/oidc/lab/callback", query))
    form = parse_qs(remote["login.example.org"].token_requests[0].content.decode())
    assert create_s256_code_challenge(form["code_verifier"][0]) == query["code_challenge"][0]


@pytest.mark.parametrize("advertised", [None, ["plain"], "S256"])
def test_pkce_omitted_unless_s256_is_advertised(client, remote, db_engine, advertised):
    link(db_engine, LAB_ISSUER, "lab-user")
    if advertised is not None:
        remote["login.example.org"].metadata["code_challenge_methods_supported"] = advertised
    query = start(client, remote, "lab")
    assert "code_challenge" not in query
    remote["login.example.org"].claims = {"nonce": query["nonce"][0], "sub": "lab-user"}
    finish(client, "/auth/hub/oidc/lab/callback", query)
    assert "code_verifier" not in parse_qs(remote["login.example.org"].token_requests[0].content.decode())


def test_pkce_omitted_for_orcid(client, remote):
    assert "code_challenge" not in start(client, remote, "orcid")


def test_in_flight_login_without_provider_key_finishes(client, remote, db_engine):
    query = start(client, remote, "orcid")
    with Session(db_engine) as session, session.begin():
        row = session.exec(select(HubLoginTransaction)).one()
        row.provider_key = None
        row.provider_code_verifier = None
    remote["orcid.org"].claims = {"nonce": query["nonce"][0], "sub": ORCID}
    assert "code" in hub_answer(finish(client, "/auth/hub/callback", query))


def test_orcid_sign_in_still_creates_the_account(client, remote, db_engine):
    assert "code" in hub_answer(orcid_sign_in(client, remote))
    assert people(db_engine) == [ORCID]
    with Session(db_engine) as session:
        assert session.exec(select(UserIdentity.issuer, UserIdentity.subject)).all() == [("https://orcid.org", ORCID)]


def test_non_orcid_login_for_a_stranger_is_denied_and_creates_nothing(client, remote, db_engine):
    answer = hub_answer(lab_sign_in(client, remote))
    assert answer == {"error": "access_denied", "state": LOGIN["state"]}
    assert people(db_engine) == []
    assert issued_codes(db_engine) == []


def test_linked_person_with_an_mfid_can_finish(client, remote, db_engine):
    unique_id = link(db_engine, LAB_ISSUER, "lab-user")
    assert "code" in hub_answer(lab_sign_in(client, remote))
    assert [row.user_unique_id for row in issued_codes(db_engine)] == [unique_id]


def test_same_subject_at_another_issuer_stays_separate(client, remote, db_engine):
    link(db_engine, "https://elsewhere.example", "lab-user")
    assert hub_answer(lab_sign_in(client, remote))["error"] == "access_denied"


def test_orcid_shaped_subject_from_another_provider_gets_no_orcid_treatment(client, remote, db_engine):
    with Session(db_engine) as session, session.begin():
        session.add(User(unique_id=ORCID, first_name="Real", last_name="Researcher"))
    assert hub_answer(lab_sign_in(client, remote, subject=ORCID))["error"] == "access_denied"
    assert issued_codes(db_engine) == []


def test_linked_service_account_is_refused(client, remote, db_engine):
    link(db_engine, LAB_ISSUER, "lab-user", is_service_account=True)
    assert hub_answer(lab_sign_in(client, remote))["error"] == "access_denied"


def test_orcid_already_on_another_account_is_denied(client, remote, db_engine):
    link(db_engine, "https://orcid.org", ORCID)
    assert hub_answer(orcid_sign_in(client, remote))["error"] == "access_denied"


def test_required_user_match_issues_a_code(client, remote, db_engine):
    unique_id = link(db_engine, LAB_ISSUER, "lab-user")
    assert "code" in hub_answer(lab_sign_in(client, remote, required_user=unique_id))


def test_required_user_mismatch_issues_no_code_and_returns_login_required(client, remote, db_engine):
    link(db_engine, LAB_ISSUER, "lab-user")
    answer = hub_answer(lab_sign_in(client, remote, required_user="someone-else"))
    assert answer == {"error": "login_required", "state": LOGIN["state"]}
    assert issued_codes(db_engine) == []


def test_step_up_with_unknown_login_creates_nothing(client, remote, db_engine):
    answer = hub_answer(orcid_sign_in(client, remote, required_user="someone-else"))
    assert answer["error"] == "login_required"
    assert people(db_engine) == []
    assert hub_answer(lab_sign_in(client, remote, required_user="someone-else"))["error"] == "login_required"
    assert people(db_engine) == []


def test_required_user_with_orcid_account(client, remote, db_engine):
    assert "code" in hub_answer(orcid_sign_in(client, remote))
    assert "code" in hub_answer(orcid_sign_in(client, remote, required_user=ORCID))
    assert hub_answer(orcid_sign_in(client, remote, required_user="someone-else"))["error"] == "login_required"


@pytest.mark.parametrize("sent, expected", [
    ("access_denied", "access_denied"),
    ("login_required", "server_error"),
    ("server_error", "server_error"),
    ("temporarily_unavailable", "temporarily_unavailable"),
    ("something_else", "server_error"),
])
def test_provider_errors_reach_the_hub_as_standard_errors(client, remote, sent, expected):
    query = start(client, remote, "lab")
    response = client.get("/auth/hub/oidc/lab/callback", params={"error": sent, "error_description": "<b>x</b>", "state": query["state"][0]}, follow_redirects=False)
    assert hub_answer(response) == {"error": expected, "state": LOGIN["state"]}
    assert remote["login.example.org"].token_requests == []


def test_callback_without_a_code_is_a_server_error(client, remote):
    query = start(client, remote, "lab")
    response = client.get("/auth/hub/oidc/lab/callback", params={"state": query["state"][0]}, follow_redirects=False)
    assert hub_answer(response)["error"] == "server_error"


def test_provider_down_at_exchange_is_temporarily_unavailable(client, remote):
    query = start(client, remote, "lab")
    remote["login.example.org"].down = True
    assert hub_answer(finish(client, "/auth/hub/oidc/lab/callback", query))["error"] == "temporarily_unavailable"


def test_bad_token_is_a_server_error(client, remote):
    query = start(client, remote, "lab")
    remote["login.example.org"].claims = {"nonce": "another", "sub": "lab-user"}
    assert hub_answer(finish(client, "/auth/hub/oidc/lab/callback", query))["error"] == "server_error"


def test_discovery_down_at_start_tells_the_hub(client, remote):
    remote["login.example.org"].down = True
    response = client.get("/auth/hub/login", params={**LOGIN, "provider": "lab"}, follow_redirects=False)
    assert hub_answer(response) == {"error": "temporarily_unavailable", "state": LOGIN["state"]}
    assert "crucible_hub_login" not in response.cookies


def test_one_provider_down_leaves_the_other_working(client, remote):
    remote["login.example.org"].down = True
    assert hub_answer(client.get("/auth/hub/login", params={**LOGIN, "provider": "lab"}, follow_redirects=False))["error"] == "temporarily_unavailable"
    assert start(client, remote, "orcid")["client_id"] == ["APP-TEST"]


def test_invalid_login_record_never_redirects(client, remote):
    for params in ({"code": "c", "state": "unknown"}, {"error": "access_denied", "state": "unknown"}, {"code": "c"}):
        response = client.get("/auth/hub/oidc/lab/callback", params=params, follow_redirects=False)
        assert response.status_code == 400
        assert "location" not in response.headers


def test_login_record_is_single_use(client, remote, db_engine):
    link(db_engine, LAB_ISSUER, "lab-user")
    query = start(client, remote, "lab")
    remote["login.example.org"].claims = {"nonce": query["nonce"][0], "sub": "lab-user"}
    assert "code" in hub_answer(finish(client, "/auth/hub/oidc/lab/callback", query))
    assert finish(client, "/auth/hub/oidc/lab/callback", query).status_code == 400


def test_expired_login_record_never_redirects(client, remote, db_engine):
    query = start(client, remote, "lab")
    with Session(db_engine) as session, session.begin():
        session.exec(select(HubLoginTransaction)).one().expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert finish(client, "/auth/hub/oidc/lab/callback", query).status_code == 400


def test_unknown_kid_refetches_keys_once(client, remote, db_engine):
    link(db_engine, LAB_ISSUER, "lab-user")
    fake = remote["login.example.org"]
    assert "code" in hub_answer(lab_sign_in(client, remote))
    rotated = RSAKey.generate_key(2048, parameters={"kid": "rotated"})
    fake.key = rotated
    fake.signing = lambda values: jwt.encode({"alg": "RS256", "kid": "rotated"}, values, rotated)
    assert "code" in hub_answer(lab_sign_in(client, remote))


def test_algorithm_not_advertised_refused(client, remote):
    other = RSAKey.generate_key(2048, parameters={"kid": "current"})
    fake = remote["login.example.org"]
    fake.key = other
    fake.metadata["id_token_signing_alg_values_supported"] = ["ES256"]
    fake.signing = lambda values: jwt.encode({"alg": "RS256", "kid": "current"}, values, other)
    assert hub_answer(lab_sign_in(client, remote))["error"] == "server_error"


def test_token_and_refresh_responses_include_auth_time(client, remote, db_engine):
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    signed_in = int(time.time()) - 5
    query = start(client, remote, "orcid", code_challenge=challenge)
    remote["orcid.org"].claims = {"nonce": query["nonce"][0], "sub": ORCID, "auth_time": signed_in}
    code = hub_answer(finish(client, "/auth/hub/callback", query))["code"]
    exchanged = client.post("/auth/hub/token", auth=("crucible-hub", "hub-secret-padded-to-at-least-32-chars00"), json={"code": code, "code_verifier": verifier, "redirect_uri": HUB_CALLBACK})
    assert exchanged.status_code == 200, exchanged.text
    body = exchanged.json()
    assert set(body) == {"access_token", "refresh_token", "token_type", "access_expires_at", "refresh_expires_at", "user_unique_id", "auth_time"}
    assert datetime.fromisoformat(body["auth_time"].replace("Z", "+00:00")) == datetime.fromtimestamp(signed_in, timezone.utc)
    refreshed = client.post("/auth/hub/refresh", auth=("crucible-hub", "hub-secret-padded-to-at-least-32-chars00"), json={"refresh_token": body["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    assert refreshed.json()["auth_time"] == body["auth_time"]


def test_auth_time_is_null_when_the_provider_gives_none(client, remote, db_engine):
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    query = start(client, remote, "orcid", code_challenge=challenge)
    remote["orcid.org"].claims = {"nonce": query["nonce"][0], "sub": ORCID}
    code = hub_answer(finish(client, "/auth/hub/callback", query))["code"]
    exchanged = client.post("/auth/hub/token", auth=("crucible-hub", "hub-secret-padded-to-at-least-32-chars00"), json={"code": code, "code_verifier": verifier, "redirect_uri": HUB_CALLBACK})
    assert exchanged.json()["auth_time"] is None


def test_hub_without_a_legacy_provider_is_allowed(client, remote, configure, db_engine):
    configure(LAB_ENTRY)
    assert "CRUCIBLE_AUTH_PROVIDERS" not in missing_required_settings()
    link(db_engine, LAB_ISSUER, "lab-user")
    query = start(client, remote)
    remote["login.example.org"].claims = {"nonce": query["nonce"][0], "sub": "lab-user"}
    assert "code" in hub_answer(finish(client, "/auth/hub/oidc/lab/callback", query))
    assert client.get("/auth/hub/callback", params={"code": "c", "state": "unknown"}, follow_redirects=False).status_code == 400


def test_failure_detail_is_not_sent_to_the_hub(client, remote):
    query = start(client, remote, "lab")
    remote["login.example.org"].claims = {"nonce": "secret-looking-value", "sub": "lab-user"}
    location = finish(client, "/auth/hub/oidc/lab/callback", query).headers["location"]
    assert "secret-looking-value" not in location
    assert set(parse_qs(urlparse(location).query)) == {"error", "state", "iss"}


@pytest.fixture
def oauth_client(db_engine, configure, remote):
    from app.api.routes import oauth as oauth_routes

    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginCode, HubLoginTransaction, HubTokenFamily, HubRefreshToken, HubAccessToken)])
    app = FastAPI()
    for router in (routes.router, auth_routes.router, oauth_routes.router, oauth_routes.discovery):
        app.include_router(router)
    app.dependency_overrides[get_engine] = lambda: db_engine
    with TestClient(app, base_url=API_URL) as test_client:
        yield test_client


OAUTH_VERIFIER = "k" * 43
OAUTH_LOGIN = {
    "response_type": "code", "client_id": "crucible-hub", "redirect_uri": HUB_CALLBACK, "state": "client-state-123456789",
    "code_challenge": base64.urlsafe_b64encode(hashlib.sha256(OAUTH_VERIFIER.encode()).digest()).rstrip(b"=").decode(), "code_challenge_method": "S256",
}


def authorize(client, **changes):
    params = {name: value for name, value in {**OAUTH_LOGIN, **changes}.items() if value is not None}
    return client.get("/oauth/authorize", params=params, follow_redirects=False)


def client_answer(response):
    assert response.status_code == 302, response.text
    assert response.headers["location"].startswith(HUB_CALLBACK + "?")
    return {name: values[0] for name, values in parse_qs(urlparse(response.headers["location"]).query).items()}


def test_standard_authorization_runs_through_to_tokens(oauth_client, remote, db_engine):
    link(db_engine, LAB_ISSUER, "lab-user")
    started = authorize(oauth_client, provider="lab")
    assert started.status_code == 302, started.text
    query = parse_qs(urlparse(started.headers["location"]).query)
    remote["login.example.org"].claims = {"nonce": query["nonce"][0], "sub": "lab-user"}
    answer = client_answer(finish(oauth_client, "/auth/hub/oidc/lab/callback", query))
    assert (answer["state"], answer["iss"]) == (OAUTH_LOGIN["state"], API_URL)
    tokens = oauth_client.post("/oauth/token", auth=("crucible-hub", "hub-secret-padded-to-at-least-32-chars00"), data={
        "grant_type": "authorization_code", "code": answer["code"], "code_verifier": OAUTH_VERIFIER, "redirect_uri": HUB_CALLBACK,
    })
    assert tokens.status_code == 200, tokens.text


@pytest.mark.parametrize("changes", [{"client_id": "nobody"}, {"redirect_uri": "https://evil.example/cb"}, {"client_id": None}])
def test_unknown_client_or_redirect_is_never_redirected_to(oauth_client, changes):
    assert authorize(oauth_client, **changes).status_code == 400


@pytest.mark.parametrize("changes,error", [
    ({"response_type": "token"}, "unsupported_response_type"),
    ({"response_type": None}, "unsupported_response_type"),
    ({"code_challenge": None, "code_challenge_method": None}, "invalid_request"),
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"code_challenge_method": None}, "invalid_request"),
])
def test_other_errors_go_back_to_the_client_with_state_and_iss(oauth_client, changes, error):
    answer = client_answer(authorize(oauth_client, **changes))
    assert answer == {"error": error, "state": OAUTH_LOGIN["state"], "iss": API_URL}


def test_repeated_parameters_are_refused(oauth_client):
    from urllib.parse import urlencode

    query = urlencode(list(OAUTH_LOGIN.items()) + [("client_id", "crucible-hub")])
    assert oauth_client.get(f"/oauth/authorize?{query}", follow_redirects=False).status_code == 400


def test_the_chooser_keeps_the_standard_request(oauth_client):
    page = authorize(oauth_client)
    assert page.status_code == 200 and "response_type=code" in page.text


def test_metadata_names_the_same_issuer_as_redirects(oauth_client):
    from authlib.oauth2.rfc8414 import AuthorizationServerMetadata

    metadata = oauth_client.get("/.well-known/oauth-authorization-server").json()
    AuthorizationServerMetadata(metadata).validate()
    assert metadata["issuer"] == API_URL and metadata["code_challenge_methods_supported"] == ["S256"]
    assert metadata["authorization_response_iss_parameter_supported"] is True


def test_no_metadata_without_clients(oauth_client, monkeypatch):
    monkeypatch.setenv("HUB_AUTH_ENABLED", "false")
    get_settings.cache_clear()
    assert oauth_client.get("/.well-known/oauth-authorization-server").status_code == 404
