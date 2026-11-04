from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App
from cachetools.func import ttl_cache

from app.api.dependencies import get_orcid_client_id, get_orcid_client_secret
from app.config import get_settings

ORCID_BROWSER = "orcid_browser"
HTTP_TIMEOUT = 10
DISCOVERY_REFRESH_SECONDS = 12 * 60 * 60
_REQUIRED_DISCOVERY_FIELDS = ("authorization_endpoint", "token_endpoint", "jwks_uri")
_UNSAFE_ALGORITHMS = ("none", "HS")


class ProviderUnavailable(Exception):
    pass


def _orcid_issuer() -> str:
    return get_settings().api.orcid_issuer


@ttl_cache(maxsize=1, ttl=DISCOVERY_REFRESH_SECONDS)
def get_oauth() -> OAuth:
    oauth = OAuth()
    oauth.register(
        ORCID_BROWSER,
        client_id=get_orcid_client_id(),
        client_secret=get_orcid_client_secret(),
        server_metadata_url=f"{_orcid_issuer()}/.well-known/openid-configuration",
        client_kwargs={"scope": "openid", "token_endpoint_auth_method": "client_secret_post", "timeout": HTTP_TIMEOUT},
    )
    return oauth


def orcid_id_token_claims() -> dict:
    return {
        "iss": {"essential": True, "values": [_orcid_issuer()]},
        "aud": {"essential": True, "values": [get_orcid_client_id()]},
    }


def audience_is_only(audience: object, client_id: str) -> bool:
    return audience in (client_id, [client_id])


def _check_discovery(metadata: dict) -> None:
    if metadata.get("issuer") != _orcid_issuer():
        raise ProviderUnavailable("discovery issuer does not match the configured issuer")
    for field in _REQUIRED_DISCOVERY_FIELDS:
        value = metadata.get(field)
        if not value or not isinstance(value, str):
            raise ProviderUnavailable(f"discovery has no usable {field}")
    advertised = metadata.get("id_token_signing_alg_values_supported")
    if advertised is not None:
        if not isinstance(advertised, list) or not all(isinstance(alg, str) for alg in advertised):
            raise ProviderUnavailable("discovery signing algorithms are malformed")
        safe = [alg for alg in advertised if not alg.startswith(_UNSAFE_ALGORITHMS)]
        if not safe:
            raise ProviderUnavailable("provider advertises no asymmetric signing algorithm")
        metadata["id_token_signing_alg_values_supported"] = safe


async def orcid_browser_client() -> StarletteOAuth2App:
    client = get_oauth().create_client(ORCID_BROWSER)
    try:
        _check_discovery(await client.load_server_metadata())
    except ProviderUnavailable:
        get_oauth.cache_clear()
        raise
    return client
