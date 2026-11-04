import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import itsdangerous
import pytest
from fastapi.testclient import TestClient
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

import app.utils.auth_utils as auth_utils
from app.api.main import app
from app.auth import browser_login
from app.auth.providers import get_oauth

ISSUER = "https://orcid.org"
ORCID = "0000-0002-1825-0097"
BROWSER_URL = "https://testserver/testapi"


class FakeOrcid:
    """Answers Authlib's real HTTP calls, so its state, nonce and token checks all run."""

    def __init__(self):
        self.key = RSAKey.generate_key(2048, parameters={"kid": "current"})
        self.published_keys = [self.key]
        self.metadata = {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/oauth/authorize",
            "token_endpoint": f"{ISSUER}/oauth/token",
            "jwks_uri": f"{ISSUER}/oauth/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        self.nonce = None
        self.claims = {}
        self.signing = None
        self.token_calls = []
        self.token_response = None
        self.discovery_calls = 0
        self.down = False

    def id_token(self):
        now = int(time.time())
        values = {"iss": ISSUER, "aud": "test-client", "sub": ORCID, "nonce": self.nonce, "iat": now, "exp": now + 300, "given_name": "Ada"}
        values.update(self.claims)
        values = {k: v for k, v in values.items() if v is not None}
        if self.signing:
            return self.signing(values)
        return jwt.encode({"alg": "RS256", "kid": self.key.kid}, values, self.key)

    def respond(self, request):
        if self.down:
            raise httpx.ConnectError("unreachable")
        path = request.url.path
        if path == "/.well-known/openid-configuration":
            self.discovery_calls += 1
            return httpx.Response(200, json=self.metadata)
        if path == "/oauth/jwks":
            return httpx.Response(200, json={"keys": [k.as_dict(private=False) for k in self.published_keys]})
        if path == "/oauth/token":
            self.token_calls.append(parse_qs(request.content.decode()))
            if self.token_response is not None:
                return httpx.Response(200, json=self.token_response)
            return httpx.Response(200, json={"access_token": "access", "token_type": "bearer", "id_token": self.id_token()})
        return httpx.Response(404)


@pytest.fixture
def orcid(monkeypatch):
    fake = FakeOrcid()

    async def handle(self, request):
        await request.aread()
        return fake.respond(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    get_oauth.cache_clear()
    yield fake
    get_oauth.cache_clear()


@pytest.fixture
def browser(db_engine, orcid):
    return TestClient(app, base_url=BROWSER_URL, follow_redirects=False)


def start_login(browser, orcid) -> str:
    query = parse_qs(urlparse(browser.get("/auth/login").headers["location"]).query)
    orcid.nonce = query["nonce"][0]
    return query["state"][0]


def session_cookie_cleared(response) -> bool:
    return any(c.startswith("crucible_login_session=") and ("max-age=0" in c.lower() or "01 jan 1970" in c.lower()) for c in response.headers.get_list("set-cookie"))


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
def test_callback_only_returns_to_local_paths(browser, orcid, cookie, target):
    state = start_login(browser, orcid)
    if cookie is not None:
        browser.cookies.set("after_login_return_to_page", cookie)
    response = browser.get("/auth/callback", params={"code": "abc", "state": state})
    assert response.status_code == 307
    assert response.headers["location"] == target


def test_each_browser_gets_a_random_state_and_nonce(browser):
    other = TestClient(app, base_url=BROWSER_URL, follow_redirects=False)
    first = parse_qs(urlparse(browser.get("/auth/login").headers["location"]).query)
    second = parse_qs(urlparse(other.get("/auth/login").headers["location"]).query)
    assert first["state"][0] != second["state"][0]
    assert first["nonce"][0] != second["nonce"][0]
    assert first["nonce"][0] != "whatever"


def test_login_keeps_the_registered_client_and_redirect(browser):
    location = browser.get("/auth/login").headers["location"]
    assert location.startswith(f"{ISSUER}/oauth/authorize?")
    query = parse_qs(urlparse(location).query)
    assert query["redirect_uri"][0].endswith("/auth")
    assert query["client_id"] == ["test-client"]
    assert query["scope"] == ["openid"]
    assert query["response_type"] == ["code"]


def test_login_session_cookie_is_protected(browser):
    cookie = browser.get("/auth/login").headers["set-cookie"].lower()
    assert cookie.startswith("crucible_login_session=")
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "max-age=600" in cookie
    assert "path=/testapi" in cookie


def test_token_exchange_sends_code_redirect_and_secret_in_the_body(browser, orcid):
    state = start_login(browser, orcid)
    browser.get("/auth", params={"code": "abc", "state": state})
    sent = orcid.token_calls[0]
    assert sent["code"] == ["abc"]
    assert sent["redirect_uri"][0].endswith("/auth")
    assert sent["client_secret"] == ["test-secret"]


@pytest.mark.parametrize("path", ["/auth/callback", "/auth"])
def test_callback_refuses_missing_or_forged_state_before_calling_orcid(browser, orcid, path):
    assert browser.get(path, params={"code": "abc"}).status_code == 400
    assert browser.get(path, params={"code": "abc", "state": "forged"}).status_code == 400
    start_login(browser, orcid)
    assert browser.get(path, params={"code": "abc", "state": "forged"}).status_code == 400
    assert browser.get(path, params={"code": "abc"}).status_code == 400
    assert not orcid.token_calls


def test_callback_refuses_a_state_from_another_browser(browser, orcid):
    attacker = TestClient(app, base_url=BROWSER_URL, follow_redirects=False)
    state = start_login(attacker, orcid)
    start_login(browser, orcid)
    response = browser.get("/auth", params={"code": "abc", "state": state})
    assert response.status_code == 400
    assert "crucible_user_token" not in response.cookies
    assert not orcid.token_calls


def test_state_works_only_once(browser, orcid):
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 307
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 400
    assert len(orcid.token_calls) == 1


def test_session_cookie_signed_with_another_key_is_refused(browser, orcid):
    state = start_login(browser, orcid)
    data = base64.b64encode(json.dumps({f"_state_orcid_browser_{state}": {"data": {"nonce": "n"}, "exp": time.time() + 60}}).encode())
    browser.cookies.set("crucible_login_session", itsdangerous.TimestampSigner("another-key").sign(data).decode())
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 400
    assert not orcid.token_calls


def test_session_older_than_ten_minutes_is_refused(browser, orcid, monkeypatch):
    state = start_login(browser, orcid)
    later = int(time.time()) + 601
    monkeypatch.setattr(itsdangerous.TimestampSigner, "get_timestamp", lambda self: later)
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 400
    assert not orcid.token_calls


def test_provider_error_is_400_without_calling_orcid(browser, orcid):
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"error": "access_denied", "state": state}).status_code == 400
    assert not orcid.token_calls


def login_redirect_query(browser):
    return parse_qs(urlparse(browser.get("/auth/login").headers["location"]).query)


def test_loading_login_twice_can_still_finish_signing_in(browser, orcid):
    first = login_redirect_query(browser)
    second = login_redirect_query(browser)
    assert first["state"] == second["state"]
    assert first["nonce"] == second["nonce"]
    orcid.nonce = first["nonce"][0]
    assert browser.get("/auth", params={"code": "abc", "state": first["state"][0]}).status_code == 307


def test_repeated_login_state_still_works_only_once(browser, orcid):
    state = start_login(browser, orcid)
    start_login(browser, orcid)
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 307
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 400


def test_a_new_sign_in_after_finishing_gets_fresh_values(browser, orcid):
    first = start_login(browser, orcid)
    first_nonce = orcid.nonce
    browser.get("/auth", params={"code": "abc", "state": first})
    assert start_login(browser, orcid) != first
    assert orcid.nonce != first_nonce


def test_a_new_sign_in_after_a_failed_exchange_gets_fresh_values(browser, orcid):
    first = start_login(browser, orcid)
    orcid.claims = {"aud": "someone-else"}
    assert browser.get("/auth", params={"code": "abc", "state": first}).status_code == 502
    assert start_login(browser, orcid) != first


def test_other_browsers_do_not_share_a_sign_in(browser, orcid):
    other = TestClient(app, base_url=BROWSER_URL, follow_redirects=False)
    assert start_login(browser, orcid) != start_login(other, orcid)


def test_pending_sign_in_older_than_its_lifetime_is_not_reused(browser, orcid, monkeypatch):
    first = start_login(browser, orcid)
    later = time.time() + 601
    monkeypatch.setattr(browser_login.time, "time", lambda: later)
    assert start_login(browser, orcid) != first


def test_forged_callback_does_not_disturb_a_sign_in_in_progress(browser, orcid):
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"code": "abc", "state": "forged"}).status_code == 400
    assert start_login(browser, orcid) == state
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 307


def test_rejected_callback_is_logged_without_its_values(browser, orcid, caplog):
    start_login(browser, orcid)
    with caplog.at_level("WARNING", logger="app.auth.browser_login"):
        browser.get("/auth", params={"code": "secret-code", "state": "secret-state"})
    assert "rejected" in caplog.text
    assert "secret-code" not in caplog.text and "secret-state" not in caplog.text


def test_session_cookie_is_cleared_after_signing_in(browser, orcid):
    state = start_login(browser, orcid)
    assert session_cookie_cleared(browser.get("/auth", params={"code": "abc", "state": state}))


@pytest.mark.parametrize("claims", [
    {"iss": "https://other.example"},
    {"iss": None},
    {"aud": "someone-else"},
    {"aud": None},
    {"aud": "someone-else", "azp": "test-client"},
    {"aud": ["someone-else"], "azp": "test-client"},
    {"aud": ["test-client", "someone-else"], "azp": "test-client"},
    {"nonce": "another-nonce"},
    {"nonce": None},
    {"exp": int(time.time()) - 3600},
    {"sub": None},
    {"sub": ""},
], ids=["wrong-iss", "no-iss", "wrong-aud", "no-aud", "other-aud-with-azp", "other-aud-list-with-azp", "extra-aud-with-azp", "wrong-nonce", "no-nonce", "expired", "no-sub", "empty-sub"])
def test_bad_id_token_claims_are_refused(browser, orcid, claims):
    orcid.claims = claims
    state = start_login(browser, orcid)
    response = browser.get("/auth", params={"code": "abc", "state": state})
    assert response.status_code == 502
    assert "crucible_user_token" not in response.cookies


def test_unsigned_identity_without_an_id_token_is_refused(browser, orcid):
    orcid.token_response = {"access_token": "access", "token_type": "bearer", "userinfo": {"sub": ORCID, "aud": "test-client"}}
    state = start_login(browser, orcid)
    response = browser.get("/auth", params={"code": "abc", "state": state})
    assert response.status_code == 502
    assert "crucible_user_token" not in response.cookies


@pytest.mark.parametrize("id_token", [None, 7, ["x"]], ids=["null", "number", "list"])
def test_malformed_id_token_is_refused(browser, orcid, id_token):
    orcid.token_response = {"access_token": "access", "token_type": "bearer", "id_token": id_token}
    state = start_login(browser, orcid)
    response = browser.get("/auth", params={"code": "abc", "state": state})
    assert response.status_code == 502
    assert "crucible_user_token" not in response.cookies


def test_a_single_audience_list_is_accepted(browser, orcid):
    orcid.claims = {"aud": ["test-client"]}
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 307


def _unsigned(values):
    def part(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    return f"{part({'alg': 'none'})}.{part(values)}."


def _hs256_with_client_secret(values):
    return jwt.encode({"alg": "HS256"}, values, OctKey.import_key("test-secret" * 4), algorithms=["HS256"])


@pytest.mark.parametrize("signing", [_unsigned, _hs256_with_client_secret], ids=["none", "hs256"])
def test_unsafe_token_algorithms_are_refused_even_when_advertised(browser, orcid, signing):
    orcid.metadata["id_token_signing_alg_values_supported"] = ["RS256", "HS256", "none"]
    orcid.signing = signing
    state = start_login(browser, orcid)
    response = browser.get("/auth", params={"code": "abc", "state": state})
    assert response.status_code == 502
    assert "crucible_user_token" not in response.cookies


def test_token_from_an_unpublished_key_is_refused(browser, orcid):
    orcid.published_keys = [RSAKey.generate_key(2048, parameters={"kid": "current"})]
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 502


def test_rotated_signing_key_is_fetched_again(browser, orcid):
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 307
    orcid.key = RSAKey.generate_key(2048, parameters={"kid": "rotated"})
    orcid.published_keys = [orcid.key]
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 307


def test_discovery_with_another_issuer_refuses_sign_in(browser, orcid):
    orcid.metadata["issuer"] = "https://orcid.org/"
    assert browser.get("/auth/login").status_code == 502


def test_only_unsafe_algorithms_refuses_sign_in(browser, orcid):
    orcid.metadata["id_token_signing_alg_values_supported"] = ["HS256"]
    assert browser.get("/auth/login").status_code == 502


@pytest.mark.parametrize("field", ["issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"])
@pytest.mark.parametrize("bad", ["missing", None, "", 7])
def test_discovery_without_a_required_field_refuses_sign_in_and_recovers(browser, orcid, field, bad):
    good = orcid.metadata[field]
    if bad == "missing":
        del orcid.metadata[field]
    else:
        orcid.metadata[field] = bad
    assert browser.get("/auth/login").status_code == 502
    orcid.metadata[field] = good
    assert browser.get("/auth/login").status_code == 302


def test_discovery_with_another_issuer_recovers_without_a_restart(browser, orcid):
    orcid.metadata["issuer"] = "https://orcid.org/"
    assert browser.get("/auth/login").status_code == 502
    orcid.metadata["issuer"] = ISSUER
    assert browser.get("/auth/login").status_code == 302


def test_discovery_with_only_unsafe_algorithms_recovers_without_a_restart(browser, orcid):
    orcid.metadata["id_token_signing_alg_values_supported"] = ["HS256"]
    assert browser.get("/auth/login").status_code == 502
    orcid.metadata["id_token_signing_alg_values_supported"] = ["RS256"]
    assert browser.get("/auth/login").status_code == 302


@pytest.mark.parametrize("algorithms", ["RS256", [7], {"alg": "RS256"}])
def test_discovery_with_malformed_algorithms_refuses_sign_in(browser, orcid, algorithms):
    orcid.metadata["id_token_signing_alg_values_supported"] = algorithms
    assert browser.get("/auth/login").status_code == 502


def test_discovery_that_is_not_an_object_refuses_sign_in_and_recovers(browser, orcid):
    good = orcid.metadata
    orcid.metadata = ["not", "metadata"]
    assert browser.get("/auth/login").status_code == 502
    orcid.metadata = good
    assert browser.get("/auth/login").status_code == 302


def test_a_good_discovery_document_is_fetched_once(browser, orcid):
    assert browser.get("/auth/login").status_code == 302
    assert browser.get("/auth/login").status_code == 302
    assert orcid.discovery_calls == 1


def test_discovery_is_refreshed_after_twelve_hours(browser, orcid):
    twelve_hours = 12 * 60 * 60
    assert browser.get("/auth/login").status_code == 302
    get_oauth.cache.expire(time.monotonic() + twelve_hours - 60)
    assert browser.get("/auth/login").status_code == 302
    assert orcid.discovery_calls == 1
    get_oauth.cache.expire(time.monotonic() + twelve_hours + 60)
    assert browser.get("/auth/login").status_code == 302
    assert orcid.discovery_calls == 2


def test_missing_client_secret_refuses_sign_in_without_a_crash(browser, orcid, monkeypatch):
    from app import config
    from app.auth import providers

    def missing():
        raise config.ConfigurationError("ORCID_CLIENT_SECRET is not set")

    monkeypatch.setattr(providers, "get_orcid_client_secret", missing)
    get_oauth.cache_clear()
    assert browser.get("/auth/login").status_code == 502


def test_state_cannot_be_used_after_orcid_reports_an_error(browser, orcid):
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"error": "access_denied", "state": state}).status_code == 400
    response = browser.get("/auth", params={"code": "abc", "state": state})
    assert response.status_code == 400
    assert "crucible_user_token" not in response.cookies
    assert orcid.token_calls == []


def test_a_new_sign_in_after_an_orcid_error_gets_fresh_values(browser, orcid):
    first = start_login(browser, orcid)
    browser.get("/auth", params={"error": "access_denied", "state": first})
    assert start_login(browser, orcid) != first


def test_an_error_with_another_state_leaves_the_sign_in_running(browser, orcid):
    state = start_login(browser, orcid)
    assert browser.get("/auth", params={"error": "access_denied", "state": "someone-elses"}).status_code == 400
    assert browser.get("/auth", params={"error": "access_denied"}).status_code == 400
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 307


def test_orcid_unreachable_at_login_is_502_and_recovers(browser, orcid):
    orcid.down = True
    assert browser.get("/auth/login").status_code == 502
    orcid.down = False
    assert browser.get("/auth/login").status_code == 302


def test_failure_message_names_no_provider(browser, orcid):
    state = start_login(browser, orcid)
    orcid.down = True
    assert browser.get("/auth", params={"code": "abc", "state": state}).json()["detail"] == "Sign-in failed; try again."


def test_orcid_unreachable_at_token_exchange_is_502(browser, orcid):
    state = start_login(browser, orcid)
    orcid.down = True
    assert browser.get("/auth", params={"code": "abc", "state": state}).status_code == 502


def test_orcid_issuer_comes_from_settings(browser, orcid, monkeypatch):
    sandbox = "https://sandbox.orcid.org"
    monkeypatch.setenv("ORCID_ISSUER", sandbox)
    orcid.metadata.update(issuer=sandbox, authorization_endpoint=f"{sandbox}/oauth/authorize")
    assert browser.get("/auth/login").headers["location"].startswith(f"{sandbox}/oauth/authorize?")


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


def test_callback_sets_a_session_cookie_that_decodes(browser, orcid):
    state = start_login(browser, orcid)
    response = browser.get("/auth/callback", params={"code": "abc", "state": state})
    claims = auth_utils.decode_token(response.cookies["crucible_user_token"])
    assert claims["orcid"] == ORCID
    assert claims["sub"] == ORCID
    assert claims["given_name"] == "Ada"
    assert "family_name" not in claims
    assert "nonce" not in claims
