import os
import urllib.parse
from functools import lru_cache

import yaml

from app.config import ConfigurationError, get_secret, get_settings
from app.utils.sql_utils import setup_engine as _setup_engine


@lru_cache
def get_engine():
    return _setup_engine(env_file=os.getenv("DB_CONF_FILE"))


@lru_cache
def get_api_config():
    api_config_file = get_settings().api.conf_file
    if not api_config_file:
        return {}
    with open(api_config_file) as f:
        return yaml.safe_load(f) or {}


def get_api_url():
    public_url = get_settings().api.public_url
    if public_url:
        return f"{public_url.rstrip('/')}{get_api_root_path()}"
    hostname = get_api_config().get("hostname")
    if not hostname:
        raise ConfigurationError("API_PUBLIC_URL is not set (the address clients use to reach this server, for example http://localhost:8080)")
    return f"https://{hostname}{get_api_root_path()}"


def get_api_path():
    configured = get_settings().api.path
    return configured if configured is not None else get_api_config().get("api_path", "")


def get_api_root_path():
    api_path = get_api_path().strip("/")
    return f"/{api_path}" if api_path else ""


def get_orcid_client_id():
    client_id = get_settings().api.orcid_client_id or get_api_config().get("orcid_client_id")
    if not client_id:
        raise ConfigurationError("ORCID_CLIENT_ID is not set")
    return client_id


def get_orcid_client_secret():
    return get_secret("ORCID_CLIENT_SECRET")


def get_redirect_uri():
    return urllib.parse.quote_plus(f"{get_api_url()}/auth")


def get_trgm_search_threshold() -> float:
    configured = get_settings().api.trgm_search_threshold
    return configured if configured is not None else float(get_api_config().get("trgm_search_threshold", 0.3))
