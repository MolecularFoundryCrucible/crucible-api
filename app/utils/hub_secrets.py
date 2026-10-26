import re
from functools import lru_cache

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError

from app.config import configured_secret, get_settings, optional_env


class HubSecretUnavailable(Exception):
    pass


def hub_auth_enabled() -> bool:
    return get_settings().hub.enabled


@lru_cache(maxsize=8)
def _read_secret_version(name: str) -> str:
    if not re.fullmatch(r"projects/[A-Za-z0-9_-]+/secrets/[A-Za-z0-9_-]+/versions/(?:[1-9][0-9]*|latest)", name):
        raise HubSecretUnavailable
    from google.cloud import secretmanager

    try:
        with secretmanager.SecretManagerServiceClient() as client:
            response = client.access_secret_version(request={"name": name}, timeout=5, retry=None)
        value = response.payload.data.decode("ascii")
    except (GoogleAPIError, GoogleAuthError, UnicodeError, ValueError):
        raise HubSecretUnavailable from None
    if not value or value.isspace() or "\n" in value or "\r" in value:
        raise HubSecretUnavailable
    return value


def get_hub_secret(environment_name: str) -> str | None:
    value = configured_secret(environment_name)
    if value is not None:
        return value
    version = optional_env(environment_name + "_VERSION")
    return _read_secret_version(version) if version else None
