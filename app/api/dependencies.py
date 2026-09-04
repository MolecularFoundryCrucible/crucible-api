import os
import urllib.parse
from functools import lru_cache

import yaml

from app.general_utils import get_secret
from app.utils.sql_utils import setup_engine as _setup_engine


@lru_cache
def get_engine():
    return _setup_engine(env_file=os.getenv("DB_CONF_FILE"))


@lru_cache
def get_api_config():
    api_config_file = os.getenv("API_CONF_FILE")
    with open(api_config_file) as f:
        return yaml.safe_load(f)


def get_api_url():
    conf = get_api_config()
    return f"https://{conf['hostname']}{get_api_root_path()}"


def get_api_path():
    return get_api_config()["api_path"]


def get_api_root_path():
    api_path = get_api_path().strip("/")
    return f"/{api_path}" if api_path else ""


def get_orcid_client_id():
    return get_api_config()["orcid_client_id"]


def get_orcid_client_secret():
    return get_secret("CLIENT_SECRET", "orcid_client_secret/versions/2")


def get_redirect_uri():
    return urllib.parse.quote_plus(f"{get_api_url()}/auth")


def get_trgm_search_threshold() -> float:
    return float(get_api_config().get("trgm_search_threshold", 0.3))
