import os
from functools import lru_cache

from app.config import ConfigurationError, get_settings
from app.utils.sql_utils import setup_engine as _setup_engine


@lru_cache
def get_engine():
    return _setup_engine(env_file=os.getenv("DB_CONF_FILE"))


def get_api_url():
    public_url = get_settings().api.public_url
    if not public_url:
        raise ConfigurationError("API_PUBLIC_URL is not set (the address clients use to reach this server, for example http://localhost:8080)")
    return f"{public_url.rstrip('/')}{get_api_root_path()}"


def get_api_path():
    return get_settings().api.path or ""


def get_api_root_path():
    api_path = get_api_path().strip("/")
    return f"/{api_path}" if api_path else ""


def get_trgm_search_threshold() -> float:
    return get_settings().api.trgm_search_threshold
