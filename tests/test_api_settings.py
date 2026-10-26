import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.api import dependencies
from app.config import ConfigurationError

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def api_env(monkeypatch):
    for name in ("API_CONF_FILE", "API_PUBLIC_URL", "API_PATH", "TRGM_SEARCH_THRESHOLD", "ORCID_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)
    dependencies.get_api_config.cache_clear()
    yield monkeypatch
    dependencies.get_api_config.cache_clear()


def test_public_url_env_wins_and_is_normalized(api_env):
    api_env.setenv("API_CONF_FILE", "app/conf/api_conf-staging.yaml")
    api_env.setenv("API_PUBLIC_URL", "http://lab.local:8080/")
    api_env.setenv("API_PATH", "/api/v3/")
    assert dependencies.get_api_root_path() == "/api/v3"
    assert dependencies.get_api_url() == "http://lab.local:8080/api/v3"


def test_yaml_still_works(api_env, tmp_path):
    conf = tmp_path / "api.yaml"
    conf.write_text('hostname: "example.org"\napi_path: "testapi-staging"\norcid_client_id: "APP-1"\ntrgm_search_threshold: 0.4\n')
    api_env.setenv("API_CONF_FILE", str(conf))
    assert dependencies.get_api_url() == "https://example.org/testapi-staging"
    assert dependencies.get_api_path() == "testapi-staging"
    assert dependencies.get_orcid_client_id() == "APP-1"
    assert dependencies.get_trgm_search_threshold() == 0.4


def test_env_values_win_over_yaml(api_env, tmp_path):
    conf = tmp_path / "api.yaml"
    conf.write_text('hostname: "example.org"\napi_path: "testapi"\norcid_client_id: "APP-1"\ntrgm_search_threshold: 0.4\n')
    api_env.setenv("API_CONF_FILE", str(conf))
    api_env.setenv("ORCID_CLIENT_ID", "APP-2")
    api_env.setenv("TRGM_SEARCH_THRESHOLD", "0.5")
    api_env.setenv("API_PUBLIC_URL", "http://lab.local")
    api_env.setenv("API_PATH", "crucible")
    assert dependencies.get_orcid_client_id() == "APP-2"
    assert dependencies.get_trgm_search_threshold() == 0.5
    assert dependencies.get_api_url() == "http://lab.local/crucible"


def test_no_config_is_fine_until_needed(api_env):
    assert dependencies.get_api_config() == {}
    assert dependencies.get_api_path() == ""
    assert dependencies.get_api_root_path() == ""
    assert dependencies.get_trgm_search_threshold() == 0.3
    with pytest.raises(ConfigurationError, match="API_PUBLIC_URL"):
        dependencies.get_api_url()
    with pytest.raises(ConfigurationError, match="ORCID_CLIENT_ID"):
        dependencies.get_orcid_client_id()


def test_root_mounted_login_redirect(api_env, client):
    response = client.get("/auth/apikey", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/login"
    assert response.cookies["after_login_return_to_page"].strip('"') == "/user_apikey"
    response = client.post("/auth/apikey/regenerate", follow_redirects=False)
    assert response.headers["location"] == "/auth/login"
    assert response.cookies["after_login_return_to_page"].strip('"') == "/auth/apikey"


def test_cursor_signing_key_setting_is_used(monkeypatch):
    from app.utils import cursor_utils

    monkeypatch.delenv("CRUCIBLE_CURSOR_SIGNING_KEYS", raising=False)
    monkeypatch.setenv("CRUCIBLE_SECRET_KEY", "main-secret")
    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEY", "cursor-secret")
    assert cursor_utils._signing_keys() == ("v1", {"v1": b"cursor-secret"})
    monkeypatch.delenv("CRUCIBLE_CURSOR_SIGNING_KEY")
    assert cursor_utils._signing_keys() == ("v1", {"v1": b"main-secret"})


def test_import_needs_no_settings(tmp_path):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO), "CRUCIBLE_SECRETS_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", "import app.api.main"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
