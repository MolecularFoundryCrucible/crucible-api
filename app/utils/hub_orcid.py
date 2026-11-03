from datetime import datetime

from app.api.dependencies import get_orcid_client_id, get_orcid_client_secret
from app.auth import oidc
from app.auth.oidc import authentication_time
from app.config import ConfigurationError, get_settings, optional_secret
from app.utils.hub_login import HubLoginConfig, HubLoginUnavailable

__all__ = ["authentication_time", "exchange_orcid_code", "orcid_authorization_url", "verify_orcid_id_token"]


def _orcid_client_id() -> str:
    return get_settings().hub.orcid_client_id or get_orcid_client_id()


def _orcid_client_secret() -> str:
    try:
        return optional_secret("HUB_AUTH_ORCID_CLIENT_SECRET") or get_orcid_client_secret()
    except ConfigurationError:
        raise HubLoginUnavailable from None


def _client(config: HubLoginConfig, client_secret: str) -> oidc.OidcClient:
    return oidc.OidcClient(
        issuer=config.orcid_issuer,
        client_id=_orcid_client_id(),
        client_secret=client_secret,
        redirect_uri=config.orcid_callback_uri,
        scope="openid",
        endpoints=oidc.orcid_endpoints(config.orcid_issuer),
        token_auth_method="client_secret_post",
    )


def _as_tuple(identity: oidc.VerifiedIdentity) -> tuple[str, str | None, str | None, datetime | None]:
    return identity.subject, identity.given_name, identity.family_name, identity.auth_time


def orcid_authorization_url(config: HubLoginConfig, state: str, nonce: str, max_age: int | None = None) -> str:
    return oidc.authorization_url(_client(config, _orcid_client_secret()), state, nonce, max_age=max_age)


def exchange_orcid_code(config: HubLoginConfig, code: str, nonce: str) -> tuple[str, str | None, str | None, datetime | None]:
    return _as_tuple(oidc.exchange_code(_client(config, _orcid_client_secret()), code, nonce))


def verify_orcid_id_token(config: HubLoginConfig, id_token: str, jwks: dict, nonce: str, access_token: str | None = None) -> tuple[str, str | None, str | None, datetime | None]:
    return _as_tuple(oidc.verify_id_token(_client(config, ""), id_token, jwks, nonce, access_token))
