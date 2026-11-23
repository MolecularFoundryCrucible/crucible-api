import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from cachetools import TTLCache
from fastapi.concurrency import run_in_threadpool
from joserfc.errors import JoseError

from app.config import ConfigurationError, OidcProvider, get_secret

HTTP_TIMEOUT = 10
ID_TOKEN_LEEWAY = 30
DISCOVERY_REFRESH_SECONDS = 12 * 60 * 60
DISCOVERY_RETRY_SECONDS = 30
_MAX_CLIENTS = 64
_REQUIRED_DISCOVERY_FIELDS = ("authorization_endpoint", "token_endpoint", "jwks_uri")
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}
_UNSAFE_ALGORITHMS = ("none", "HS")

_clients: TTLCache = TTLCache(maxsize=_MAX_CLIENTS, ttl=DISCOVERY_REFRESH_SECONDS)
_recent_failures: TTLCache = TTLCache(maxsize=_MAX_CLIENTS, ttl=DISCOVERY_RETRY_SECONDS)


class ProviderUnavailable(Exception):
    pass


TOKEN_FAILURES = (ProviderUnavailable, ConfigurationError, OAuthError, JoseError, httpx.HTTPError, TypeError, ValueError, KeyError)


@dataclass(frozen=True)
class VerifiedIdentity:
    subject: str
    given_name: str | None
    family_name: str | None
    auth_time: datetime | None
    label: str | None = None


def forget_clients() -> None:
    _clients.clear()
    _recent_failures.clear()


def id_token_claims(issuer: str, client_id: str) -> dict:
    return {
        "iss": {"essential": True, "values": [issuer]},
        "aud": {"essential": True, "values": [client_id]},
    }


def audience_is_only(audience: object, client_id: str) -> bool:
    return audience in (client_id, [client_id])


def _check_discovery(metadata: dict, issuer: str) -> None:
    if metadata.get("issuer") != issuer:
        raise ProviderUnavailable("discovery issuer does not match the configured issuer")
    for field in _REQUIRED_DISCOVERY_FIELDS:
        value = metadata.get(field)
        if not value or not isinstance(value, str):
            raise ProviderUnavailable(f"discovery has no usable {field}")
    for field in (*_REQUIRED_DISCOVERY_FIELDS, "userinfo_endpoint"):
        if field in metadata and not _secure_endpoint(metadata[field], issuer):
            raise ProviderUnavailable(f"discovery {field} is not an https address")
    advertised = metadata.get("id_token_signing_alg_values_supported")
    if advertised is not None:
        if not isinstance(advertised, list) or not all(isinstance(alg, str) for alg in advertised):
            raise ProviderUnavailable("discovery signing algorithms are malformed")
        safe = [alg for alg in advertised if not alg.startswith(_UNSAFE_ALGORITHMS)]
        if not safe:
            raise ProviderUnavailable("provider advertises no asymmetric signing algorithm")
        metadata["id_token_signing_alg_values_supported"] = safe


def _secure_endpoint(address: object, issuer: str) -> bool:
    """https, as OpenID Connect Discovery requires; http only on loopback for a loopback issuer, as a local test provider uses."""
    if not isinstance(address, str):
        return False
    parsed = urlparse(address)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    return parsed.scheme == "http" and parsed.hostname in _LOOPBACK and urlparse(issuer).hostname in _LOOPBACK


def _registered(name: str, issuer: str, client_id: str, client_secret: str, scope: str, auth_method: str) -> StarletteOAuth2App:
    oauth = OAuth()
    oauth.register(
        name,
        client_id=client_id,
        client_secret=client_secret,
        server_metadata_url=f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        client_kwargs={"scope": scope, "token_endpoint_auth_method": auth_method, "timeout": HTTP_TIMEOUT},
    )
    return oauth.create_client(name)


def _provider_app(provider: OidcProvider) -> StarletteOAuth2App:
    client = _clients.get(provider.key)
    if client is None:
        client = _registered(provider.key, provider.issuer, provider.client_id, get_secret(provider.secret_name), provider.scopes, provider.token_endpoint_auth_method)
        _clients[provider.key] = client
    return client


async def _with_discovery(name: str, client: StarletteOAuth2App, issuer: str) -> StarletteOAuth2App:
    if name in _recent_failures:
        raise ProviderUnavailable("waiting before trying discovery again")
    try:
        _check_discovery(await client.load_server_metadata(), issuer)
    except (ProviderUnavailable, httpx.HTTPError, ValueError) as error:
        _clients.pop(name, None)
        _recent_failures[name] = True
        raise ProviderUnavailable(type(error).__name__) from None
    return client


async def new_code_verifier(provider: OidcProvider) -> str | None:
    """A PKCE verifier when the provider advertises S256, which ORCID's discovery document does not."""
    client = await _with_discovery(provider.key, await run_in_threadpool(_provider_app, provider), provider.issuer)
    advertised = client.server_metadata.get("code_challenge_methods_supported")
    return secrets.token_urlsafe(64) if isinstance(advertised, list) and "S256" in advertised else None


async def authorization_url(provider: OidcProvider, callback_uri: str, state: str, nonce: str, max_age: int | None = None, code_verifier: str | None = None) -> str:
    client = await _with_discovery(provider.key, await run_in_threadpool(_provider_app, provider), provider.issuer)
    extra = {} if max_age is None else {"max_age": str(max_age), **({"prompt": "login"} if max_age == 0 else {})}
    if code_verifier is not None:
        extra |= {"code_challenge": create_s256_code_challenge(code_verifier), "code_challenge_method": "S256"}
    created = await client.create_authorization_url(callback_uri, state=state, nonce=nonce, **extra)
    return created["url"]


class IssuerMismatch(Exception):
    pass


async def check_response_issuer(provider: OidcProvider, iss: str | None) -> None:
    """RFC 9207 mix-up defense: an authorization response's `iss` must name this provider, and a
    provider that advertises the parameter must send it."""
    client = await _with_discovery(provider.key, await run_in_threadpool(_provider_app, provider), provider.issuer)
    if iss is None:
        if client.server_metadata.get("authorization_response_iss_parameter_supported") is True:
            raise IssuerMismatch("missing iss")
        return
    if iss != client.server_metadata.get("issuer"):
        raise IssuerMismatch("unexpected iss")


async def verified_identity(provider: OidcProvider, callback_uri: str, code: str, nonce: str, code_verifier: str | None = None) -> VerifiedIdentity:
    client = await _with_discovery(provider.key, await run_in_threadpool(_provider_app, provider), provider.issuer)
    token = await client.fetch_access_token(redirect_uri=callback_uri, code=code, **({} if code_verifier is None else {"code_verifier": code_verifier}))
    if "id_token" not in token:
        raise ValueError("token response has no ID token")
    claims = await client.parse_id_token(token, nonce, claims_options=id_token_claims(provider.issuer, provider.client_id), leeway=ID_TOKEN_LEEWAY)
    if not audience_is_only(claims.get("aud"), provider.client_id):
        raise ValueError("ID token lists another audience")
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("ID token has no subject")
    label = next((value for value in (claims.get("email"), claims.get("preferred_username"), claims.get("name")) if isinstance(value, str) and value), None)
    return VerifiedIdentity(subject, claims.get("given_name"), claims.get("family_name"), authentication_time(claims.get("auth_time")), label[:255] if label else None)


def authentication_time(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    moment = datetime.fromtimestamp(value, timezone.utc)
    return moment if moment <= datetime.now(timezone.utc) + timedelta(seconds=30) else None
