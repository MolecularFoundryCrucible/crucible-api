import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
import yaml
from cachetools import TTLCache
from joserfc import jwt
from joserfc.jwk import OctKey, RSAKey

from app import config
from app.auth import providers
from app.config import ConfigurationError, get_settings, missing_required_settings

ORCID_ENTRY = {
    "key": "orcid",
    "display_name": "ORCID",
    "issuer": "https://orcid.org",
    "client_id": "APP-TEST",
    "scopes": "openid",
    "token_endpoint_auth_method": "client_secret_post",
    "registration": "open",
    "legacy_callback": True,
}
LAB_ENTRY = {
    "key": "lab",
    "display_name": "Example Lab",
    "issuer": "https://login.example.org/realms/lab",
    "client_id": "crucible",
    "scopes": "openid profile",
    "token_endpoint_auth_method": "client_secret_basic",
    "registration": "closed",
    "legacy_callback": False,
}
HUB_ENVIRONMENT = {"HUB_AUTH_ENABLED": "true", "HUB_AUTH_CLIENT_SECRET": "hub-secret", "HUB_AUTH_CALLBACK_URI": "https://hub.example/callback"}
CALLBACK = "https://api.example/auth/hub/callback"


def set_providers(monkeypatch, *entries):
    monkeypatch.setenv("CRUCIBLE_AUTH_PROVIDERS", json.dumps(list(entries)))
    get_settings.cache_clear()


def provider_problem(monkeypatch, entry):
    set_providers(monkeypatch, entry)
    with pytest.raises(ConfigurationError) as caught:
        get_settings()
    return str(caught.value)


def test_settings_read_every_field_of_the_list(monkeypatch):
    set_providers(monkeypatch, ORCID_ENTRY, LAB_ENTRY)
    assert [provider.model_dump() for provider in get_settings().auth.providers] == [ORCID_ENTRY, LAB_ENTRY]


@pytest.mark.parametrize("field", sorted(ORCID_ENTRY))
def test_every_field_is_required(monkeypatch, field):
    entry = {name: value for name, value in ORCID_ENTRY.items() if name != field}
    assert f"CRUCIBLE_AUTH_PROVIDERS.0.{field}" in provider_problem(monkeypatch, entry)


def test_unknown_field_is_refused(monkeypatch):
    assert "CRUCIBLE_AUTH_PROVIDERS.0.client_secret" in provider_problem(monkeypatch, {**ORCID_ENTRY, "client_secret": "should-not-be-here"})


def test_invalid_json_is_refused_without_repeating_it(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_PROVIDERS", '[{"client_secret": "leaked-value"')
    with pytest.raises(ConfigurationError) as caught:
        get_settings()
    assert "CRUCIBLE_AUTH_PROVIDERS" in str(caught.value)
    assert "leaked-value" not in str(caught.value)


@pytest.mark.parametrize("change", [
    {"key": "Has-Capitals"},
    {"key": "orcid_browser"},
    {"issuer": "http://login.example.org"},
    {"issuer": "ftp://login.example.org"},
    {"issuer": "https://login.example.org?tenant=1"},
    {"issuer": "https://user@login.example.org"},
    {"scopes": "profile"},
    {"token_endpoint_auth_method": "none"},
    {"registration": "maybe"},
])
def test_bad_values_are_refused(monkeypatch, change):
    with pytest.raises(ConfigurationError):
        set_providers(monkeypatch, {**LAB_ENTRY, **change})
        get_settings()


def test_http_is_allowed_for_localhost(monkeypatch):
    set_providers(monkeypatch, {**LAB_ENTRY, "issuer": "http://localhost:5556/dex"})
    assert get_settings().auth.providers[0].issuer == "http://localhost:5556/dex"


@pytest.mark.parametrize("issuer, expected", [
    ("https://orcid.org", True),
    ("https://sandbox.orcid.org", True),
    ("https://orcid.org/", False),
    ("https://orcid.org.example", False),
    ("https://login.example.org", False),
])
def test_orcid_status_comes_only_from_the_exact_issuer(monkeypatch, issuer, expected):
    set_providers(monkeypatch, {**LAB_ENTRY, "issuer": issuer})
    assert get_settings().auth.providers[0].is_orcid is expected


def test_old_subject_is_orcid_field_is_refused(monkeypatch):
    assert "subject_is_orcid" in provider_problem(monkeypatch, {**ORCID_ENTRY, "subject_is_orcid": True})


def test_legacy_callback_needs_orcid(monkeypatch):
    assert "legacy_callback" in provider_problem(monkeypatch, {**LAB_ENTRY, "legacy_callback": True})


@pytest.mark.parametrize("second", [
    {**LAB_ENTRY, "key": "orcid"},
    {**ORCID_ENTRY, "key": "other"},
])
def test_duplicates_are_refused(monkeypatch, second):
    set_providers(monkeypatch, ORCID_ENTRY, second)
    with pytest.raises(ConfigurationError):
        get_settings()


def test_hub_without_a_provider_list_is_a_missing_setting(monkeypatch):
    for name, value in HUB_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    assert "CRUCIBLE_AUTH_PROVIDERS" in missing_required_settings()


def test_no_provider_is_implied_by_the_older_orcid_settings(monkeypatch):
    monkeypatch.setenv("ORCID_CLIENT_ID", "APP-TEST")
    monkeypatch.setenv("ORCID_CLIENT_SECRET", "secret")
    assert get_settings().auth.providers == []


def test_provider_without_its_secret_is_a_missing_setting(monkeypatch):
    set_providers(monkeypatch, LAB_ENTRY)
    assert "OIDC_LAB_CLIENT_SECRET" in missing_required_settings()


@pytest.mark.parametrize("way", ["env", "file", "directory"])
def test_provider_secret_is_read_each_way(monkeypatch, tmp_path, way):
    set_providers(monkeypatch, LAB_ENTRY)
    if way == "env":
        monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET", "the-secret")
    elif way == "file":
        (tmp_path / "pointed").write_text("the-secret\n")
        monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET_FILE", str(tmp_path / "pointed"))
    else:
        (tmp_path / "OIDC_LAB_CLIENT_SECRET").write_text("the-secret\n")
        monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(tmp_path))
    assert config.get_secret("OIDC_LAB_CLIENT_SECRET") == "the-secret"
    assert "OIDC_LAB_CLIENT_SECRET" in config.secrets_in_use()
    assert "OIDC_LAB_CLIENT_SECRET" not in missing_required_settings()


def test_provider_secret_from_secret_manager(monkeypatch):
    version = "projects/p/secrets/s/versions/1"
    set_providers(monkeypatch, LAB_ENTRY)
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET_VERSION", version)
    monkeypatch.setattr(config, "get_gcs_secret", lambda name: "from-manager")
    assert "OIDC_LAB_CLIENT_SECRET" not in missing_required_settings()
    assert config.get_secret("OIDC_LAB_CLIENT_SECRET") == "from-manager"


def test_malformed_secret_version_is_a_missing_setting(monkeypatch):
    set_providers(monkeypatch, LAB_ENTRY)
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET_VERSION", "not-a-version")
    assert "OIDC_LAB_CLIENT_SECRET" in missing_required_settings()


def test_provider_secret_given_two_ways_is_refused(monkeypatch, tmp_path):
    set_providers(monkeypatch, LAB_ENTRY)
    (tmp_path / "pointed").write_text("one")
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET", "two")
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET_FILE", str(tmp_path / "pointed"))
    with pytest.raises(ConfigurationError, match="more than one way"):
        config.get_secret("OIDC_LAB_CLIENT_SECRET")


def test_provider_secret_must_be_one_line(monkeypatch):
    set_providers(monkeypatch, LAB_ENTRY)
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET", "two\nlines")
    with pytest.raises(ConfigurationError, match="one line"):
        config.get_secret("OIDC_LAB_CLIENT_SECRET")


def test_secret_of_an_unlisted_provider_is_not_a_secret_setting(monkeypatch):
    set_providers(monkeypatch, LAB_ENTRY)
    with pytest.raises(KeyError):
        config.configured_secret("OIDC_OTHER_CLIENT_SECRET")


REPO = Path(__file__).resolve().parents[1]


def test_published_example_is_accepted(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_PROVIDERS", (REPO / "deploy/compose/auth-providers.example.json").read_text())
    listed = get_settings().auth.providers
    assert [provider.key for provider in listed] == ["orcid", "lab"]
    assert listed[0].legacy_callback and listed[0].is_orcid


def test_compose_bundle_lists_orcid_explicitly(monkeypatch):
    environment = yaml.safe_load((REPO / "deploy/compose/compose.hub.yaml").read_text())["services"]["api"]["environment"]
    value = re.sub(r"\$\{ORCID_CLIENT_ID:\?[^}]*\}", "APP-TEST", environment["CRUCIBLE_AUTH_PROVIDERS"])
    value = re.sub(r"\$\{ORCID_ISSUER:-([^}]*)\}", r"\1", value)
    monkeypatch.setenv("CRUCIBLE_AUTH_PROVIDERS", value)
    provider = get_settings().auth.providers[0]
    assert (provider.key, provider.client_id, provider.issuer, provider.token_endpoint_auth_method) == ("orcid", "APP-TEST", "https://orcid.org", "client_secret_post")


class FakeProvider:
    def __init__(self, issuer="https://orcid.org", client_id="APP-TEST"):
        self.issuer = issuer
        self.client_id = client_id
        self.key = RSAKey.generate_key(2048, parameters={"kid": "current"})
        self.metadata = {
            "issuer": issuer,
            "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token",
            "jwks_uri": f"{issuer}/oauth/jwks",
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        self.claims = {}
        self.signing = None
        self.discovery_calls = 0
        self.token_requests = []
        self.down = False

    def id_token(self):
        now = int(time.time())
        values = {"iss": self.issuer, "aud": self.client_id, "sub": "0000-0002-1825-0097", "nonce": "the-nonce", "iat": now, "exp": now + 300, "given_name": "Ada", "family_name": "Lovelace", **self.claims}
        if self.signing:
            return self.signing(values)
        return jwt.encode({"alg": "RS256", "kid": "current"}, values, self.key)

    def respond(self, request):
        if self.down:
            raise httpx.ConnectError("unreachable")
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            self.discovery_calls += 1
            return httpx.Response(200, json=self.metadata)
        if path.endswith("/oauth/jwks"):
            return httpx.Response(200, json={"keys": [self.key.as_dict(private=False)]})
        if path.endswith("/oauth/token"):
            self.token_requests.append(request)
            return httpx.Response(200, json={"access_token": "access", "token_type": "bearer", "id_token": self.id_token()})
        return httpx.Response(404)


@pytest.fixture
def remote(monkeypatch):
    fakes = {"orcid.org": FakeProvider(), "login.example.org": FakeProvider("https://login.example.org/realms/lab", "crucible")}

    async def handle(self, request):
        await request.aread()
        return fakes[request.url.host].respond(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    monkeypatch.setenv("OIDC_ORCID_CLIENT_SECRET", "orcid-secret")
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET", "lab-secret")
    set_providers(monkeypatch, ORCID_ENTRY, LAB_ENTRY)
    providers.forget_clients()
    yield fakes
    providers.forget_clients()


def provider(key):
    return next(item for item in get_settings().auth.providers if item.key == key)


def query_of(url):
    return parse_qs(urlparse(url).query)


@pytest.mark.anyio
async def test_authorization_url_comes_from_discovery_and_uses_the_callers_values(remote):
    url = await providers.authorization_url(provider("orcid"), CALLBACK, "api-state", "api-nonce")
    assert url.startswith("https://orcid.org/oauth/authorize?")
    query = query_of(url)
    assert query["state"] == ["api-state"]
    assert query["nonce"] == ["api-nonce"]
    assert query["redirect_uri"] == [CALLBACK]
    assert query["client_id"] == ["APP-TEST"]
    assert query["scope"] == ["openid"]
    assert query["response_type"] == ["code"]
    assert "max_age" not in query and "prompt" not in query


@pytest.mark.anyio
async def test_recent_sign_in_parameters(remote):
    forced = query_of(await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n", 0))
    assert forced["max_age"] == ["0"] and forced["prompt"] == ["login"]
    bounded = query_of(await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n", 300))
    assert bounded["max_age"] == ["300"] and "prompt" not in bounded


@pytest.mark.anyio
async def test_exchange_returns_the_verified_identity(remote):
    signed_in = int(time.time()) - 5
    remote["orcid.org"].claims = {"auth_time": signed_in}
    identity = await providers.verified_identity(provider("orcid"), CALLBACK, "the-code", "the-nonce")
    assert identity == providers.VerifiedIdentity("0000-0002-1825-0097", "Ada", "Lovelace", datetime.fromtimestamp(signed_in, timezone.utc))
    form = parse_qs(remote["orcid.org"].token_requests[0].content.decode())
    assert form["code"] == ["the-code"]
    assert form["redirect_uri"] == [CALLBACK]
    assert form["client_id"] == ["APP-TEST"]
    assert form["client_secret"] == ["orcid-secret"]


@pytest.mark.anyio
async def test_audience_as_a_one_item_list_is_accepted(remote):
    remote["orcid.org"].claims = {"aud": ["APP-TEST"]}
    assert (await providers.verified_identity(provider("orcid"), CALLBACK, "the-code", "the-nonce")).subject == "0000-0002-1825-0097"


@pytest.mark.anyio
async def test_token_auth_method_comes_from_the_provider(remote):
    remote["login.example.org"].claims = {"sub": "lab-user"}
    identity = await providers.verified_identity(provider("lab"), CALLBACK, "the-code", "the-nonce")
    assert identity.subject == "lab-user"
    request = remote["login.example.org"].token_requests[0]
    assert request.headers["authorization"].startswith("Basic ")
    assert "client_secret" not in parse_qs(request.content.decode())


@pytest.mark.anyio
@pytest.mark.parametrize("claims", [
    {"iss": "https://elsewhere.example"},
    {"aud": "someone-else"},
    {"aud": ["APP-TEST", "someone-else"]},
    {"nonce": "another-nonce"},
    {"exp": int(time.time()) - 3600},
])
async def test_wrong_tokens_are_refused(remote, claims):
    remote["orcid.org"].claims = claims
    with pytest.raises(providers.TOKEN_FAILURES):
        await providers.verified_identity(provider("orcid"), CALLBACK, "the-code", "the-nonce")


@pytest.mark.anyio
async def test_token_signed_with_another_key_is_refused(remote):
    other = RSAKey.generate_key(2048, parameters={"kid": "current"})
    remote["orcid.org"].signing = lambda values: jwt.encode({"alg": "RS256", "kid": "current"}, values, other)
    with pytest.raises(providers.TOKEN_FAILURES):
        await providers.verified_identity(provider("orcid"), CALLBACK, "the-code", "the-nonce")


@pytest.mark.anyio
async def test_hs256_signed_with_the_client_secret_is_refused(remote):
    secret = OctKey.import_key("orcid-secret" * 4)
    remote["orcid.org"].signing = lambda values: jwt.encode({"alg": "HS256"}, values, secret, algorithms=["HS256"])
    with pytest.raises(providers.TOKEN_FAILURES):
        await providers.verified_identity(provider("orcid"), CALLBACK, "the-code", "the-nonce")


@pytest.mark.anyio
async def test_response_without_an_id_token_is_refused(remote, monkeypatch):
    fake = remote["orcid.org"]
    original = fake.respond
    monkeypatch.setattr(fake, "respond", lambda request: httpx.Response(200, json={"access_token": "a", "token_type": "bearer"}) if request.url.path.endswith("/oauth/token") else original(request))
    with pytest.raises(providers.TOKEN_FAILURES):
        await providers.verified_identity(provider("orcid"), CALLBACK, "the-code", "the-nonce")


@pytest.mark.parametrize("value, expected", [
    (None, None),
    (True, None),
    ("1700000000", None),
    (1700000000, datetime.fromtimestamp(1700000000, timezone.utc)),
    (int(time.time()) + 3600, None),
])
def test_authentication_time(value, expected):
    assert providers.authentication_time(value) == expected


@pytest.mark.anyio
async def test_issuer_mismatch_marks_only_that_provider_unavailable(remote):
    remote["login.example.org"].metadata["issuer"] = "https://login.example.org/realms/lab/"
    with pytest.raises(providers.ProviderUnavailable):
        await providers.authorization_url(provider("lab"), CALLBACK, "s", "n")
    assert (await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n")).startswith("https://orcid.org/")


@pytest.mark.anyio
async def test_missing_secret_on_one_provider_leaves_the_other_working(remote, monkeypatch):
    monkeypatch.delenv("OIDC_LAB_CLIENT_SECRET")
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError):
        await providers.authorization_url(provider("lab"), CALLBACK, "s", "n")
    assert (await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n")).startswith("https://orcid.org/")


@pytest.mark.anyio
async def test_unreachable_discovery_waits_before_trying_again_then_recovers(remote, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(providers, "_recent_failures", TTLCache(maxsize=64, ttl=providers.DISCOVERY_RETRY_SECONDS, timer=lambda: clock[0]))
    fake = remote["orcid.org"]
    fake.down = True
    with pytest.raises(providers.ProviderUnavailable):
        await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n")
    fake.down = False
    with pytest.raises(providers.ProviderUnavailable):
        await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n")
    assert fake.discovery_calls == 0
    clock[0] += providers.DISCOVERY_RETRY_SECONDS + 1
    assert (await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n")).startswith("https://orcid.org/")
    assert fake.discovery_calls == 1


@pytest.mark.anyio
async def test_discovery_is_fetched_once_while_it_is_good(remote):
    for _ in range(3):
        await providers.authorization_url(provider("orcid"), CALLBACK, "s", "n")
    assert remote["orcid.org"].discovery_calls == 1


@pytest.mark.anyio
async def test_discovery_address_drops_a_trailing_slash_but_the_issuer_stays_exact(monkeypatch):
    issuer = "https://login.example.org/application/o/crucible/"
    fake = FakeProvider(issuer, "crucible")
    paths = []

    async def handle(self, request):
        await request.aread()
        paths.append(request.url.path)
        return fake.respond(request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    monkeypatch.setenv("OIDC_LAB_CLIENT_SECRET", "lab-secret")
    set_providers(monkeypatch, {**LAB_ENTRY, "issuer": issuer})
    await providers.authorization_url(get_settings().auth.providers[0], CALLBACK, "s", "n")
    assert paths == ["/application/o/crucible/.well-known/openid-configuration"]
    fake.metadata["issuer"] = issuer.rstrip("/")
    providers.forget_clients()
    with pytest.raises(providers.ProviderUnavailable):
        await providers.authorization_url(get_settings().auth.providers[0], CALLBACK, "s", "n")
