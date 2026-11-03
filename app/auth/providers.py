from functools import lru_cache

from authlib.integrations.starlette_client import OAuth, StarletteOAuth2App

from app.api.dependencies import get_orcid_client_id, get_orcid_client_secret
from app.config import get_settings

ORCID_BROWSER = "orcid_browser"
HTTP_TIMEOUT = 10
_UNSAFE_ALGORITHMS = ("none", "HS")


class ProviderUnavailable(Exception):
    pass


def _orcid_issuer() -> str:
    return get_settings().api.orcid_issuer


@lru_cache
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


async def orcid_browser_client() -> StarletteOAuth2App:
    client = get_oauth().create_client(ORCID_BROWSER)
    metadata = await client.load_server_metadata()
    if metadata.get("issuer") != _orcid_issuer():
        raise ProviderUnavailable("discovery issuer does not match the configured issuer")
    advertised = metadata.get("id_token_signing_alg_values_supported")
    if advertised is not None:
        safe = [alg for alg in advertised if not alg.startswith(_UNSAFE_ALGORITHMS)]
        if not safe:
            raise ProviderUnavailable("provider advertises no asymmetric signing algorithm")
        metadata["id_token_signing_alg_values_supported"] = safe
    return client
