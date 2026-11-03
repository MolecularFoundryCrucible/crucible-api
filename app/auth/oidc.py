from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests
from authlib.integrations.requests_client import OAuth2Session
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

HTTP_TIMEOUT = 10


class InvalidSignIn(Exception):
    pass


@dataclass(frozen=True)
class OidcEndpoints:
    authorization: str
    token: str
    jwks: str


@dataclass(frozen=True)
class OidcClient:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str
    endpoints: OidcEndpoints
    token_auth_method: str


@dataclass(frozen=True)
class VerifiedIdentity:
    issuer: str
    subject: str
    given_name: str | None
    family_name: str | None
    auth_time: datetime | None


def orcid_endpoints(issuer: str) -> OidcEndpoints:
    return OidcEndpoints(f"{issuer}/oauth/authorize", f"{issuer}/oauth/token", f"{issuer}/oauth/jwks")


def _session(client: OidcClient) -> OAuth2Session:
    return OAuth2Session(
        client_id=client.client_id,
        client_secret=client.client_secret,
        token_endpoint_auth_method=client.token_auth_method,
        scope=client.scope,
        default_timeout=HTTP_TIMEOUT,
    )


def authorization_url(client: OidcClient, state: str, nonce: str, *, max_age: int | None = None) -> str:
    reauthentication = {} if max_age is None else {"max_age": str(max_age), **({"prompt": "login"} if max_age == 0 else {})}
    with _session(client) as session:
        url, returned_state = session.create_authorization_url(
            client.endpoints.authorization,
            state=state,
            redirect_uri=client.redirect_uri,
            nonce=nonce,
            **reauthentication,
        )
    if returned_state != state:
        raise InvalidSignIn
    return url


def exchange_code(client: OidcClient, code: str, nonce: str) -> VerifiedIdentity:
    if not code or len(code) > 2048:
        raise InvalidSignIn
    with _session(client) as session:
        token = session.fetch_token(
            client.endpoints.token,
            code=code,
            redirect_uri=client.redirect_uri,
            allow_redirects=False,
        )
    id_token = token.get("id_token")
    if not isinstance(id_token, str):
        raise InvalidSignIn
    response = requests.get(client.endpoints.jwks, timeout=HTTP_TIMEOUT, allow_redirects=False)
    response.raise_for_status()
    return verify_id_token(client, id_token, response.json(), nonce, token.get("access_token"))


def verify_id_token(client: OidcClient, id_token: str, jwks: dict, nonce: str, access_token: str | None = None) -> VerifiedIdentity:
    keys = KeySet.import_key_set(jwks)
    verified = jwt.decode(id_token, keys, algorithms=["RS256"])
    claims = CodeIDToken(
        verified.claims,
        verified.header,
        options={
            "iss": {"essential": True, "value": client.issuer},
            "aud": {"essential": True, "value": client.client_id},
        },
        params={
            "nonce": nonce,
            "client_id": client.client_id,
            "access_token": access_token,
        },
    )
    claims.validate(leeway=30)
    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject:
        raise InvalidSignIn
    return VerifiedIdentity(client.issuer, subject, claims.get("given_name"), claims.get("family_name"), authentication_time(claims.get("auth_time")))


def authentication_time(value) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    moment = datetime.fromtimestamp(value, timezone.utc)
    return moment if moment <= datetime.now(timezone.utc) + timedelta(seconds=30) else None
