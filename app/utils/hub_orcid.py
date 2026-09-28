import os

import requests
from authlib.integrations.requests_client import OAuth2Session
from authlib.oidc.core import CodeIDToken
from joserfc import jwt
from joserfc.jwk import KeySet

from app.api.dependencies import get_orcid_client_id, get_orcid_client_secret
from app.utils.hub_login import HubLoginConfig, HubLoginUnavailable, InvalidHubLogin
from app.utils.hub_secrets import HubSecretUnavailable, get_hub_secret


def _orcid_client_id() -> str:
    return os.getenv("HUB_AUTH_ORCID_CLIENT_ID") or get_orcid_client_id()


def _orcid_client_secret() -> str:
    try:
        return get_hub_secret("HUB_AUTH_ORCID_CLIENT_SECRET") or get_orcid_client_secret()
    except HubSecretUnavailable:
        raise HubLoginUnavailable from None


def _client(config: HubLoginConfig) -> OAuth2Session:
    return OAuth2Session(
        client_id=_orcid_client_id(),
        client_secret=_orcid_client_secret(),
        token_endpoint_auth_method="client_secret_post",
        scope="openid",
        default_timeout=10,
    )


def orcid_authorization_url(config: HubLoginConfig, state: str, nonce: str) -> str:
    with _client(config) as client:
        url, returned_state = client.create_authorization_url(
            f"{config.orcid_issuer}/oauth/authorize",
            state=state,
            redirect_uri=config.orcid_callback_uri,
            nonce=nonce,
        )
    if returned_state != state:
        raise InvalidHubLogin
    return url


def exchange_orcid_code(config: HubLoginConfig, code: str, nonce: str) -> tuple[str, str | None, str | None]:
    if not code or len(code) > 2048:
        raise InvalidHubLogin
    with _client(config) as client:
        token = client.fetch_token(
            f"{config.orcid_issuer}/oauth/token",
            code=code,
            redirect_uri=config.orcid_callback_uri,
            allow_redirects=False,
        )
    id_token = token.get("id_token")
    if not isinstance(id_token, str):
        raise InvalidHubLogin
    response = requests.get(f"{config.orcid_issuer}/oauth/jwks", timeout=10, allow_redirects=False)
    response.raise_for_status()
    return verify_orcid_id_token(config, id_token, response.json(), nonce, token.get("access_token"))


def verify_orcid_id_token(config: HubLoginConfig, id_token: str, jwks: dict, nonce: str, access_token: str | None = None) -> tuple[str, str | None, str | None]:
    keys = KeySet.import_key_set(jwks)
    verified = jwt.decode(id_token, keys, algorithms=["RS256"])
    claims = CodeIDToken(
        verified.claims,
        verified.header,
        options={
            "iss": {"essential": True, "value": config.orcid_issuer},
            "aud": {"essential": True, "value": _orcid_client_id()},
        },
        params={
            "nonce": nonce,
            "client_id": _orcid_client_id(),
            "access_token": access_token,
        },
    )
    claims.validate(leeway=30)
    return claims["sub"], claims.get("given_name"), claims.get("family_name")
