import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.api import dependencies
from app.config import ConfigurationError, get_settings

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def api_env(monkeypatch):
    for name in ("API_CONF_FILE", "API_PUBLIC_URL", "API_PATH", "TRGM_SEARCH_THRESHOLD"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_public_url_and_path_are_normalized(api_env):
    api_env.setenv("API_PUBLIC_URL", "http://lab.local:8080/")
    api_env.setenv("API_PATH", "/api/v3/")
    api_env.setenv("TRGM_SEARCH_THRESHOLD", "0.5")
    assert dependencies.get_api_root_path() == "/api/v3"
    assert dependencies.get_api_url() == "http://lab.local:8080/api/v3"
    assert dependencies.get_trgm_search_threshold() == 0.5


def test_the_removed_yaml_file_setting_is_ignored(api_env, tmp_path):
    conf = tmp_path / "api.yaml"
    conf.write_text('hostname: "example.org"\napi_path: "from-yaml"\ntrgm_search_threshold: 0.4\n')
    api_env.setenv("API_CONF_FILE", str(conf))
    assert dependencies.get_api_path() == ""
    assert dependencies.get_trgm_search_threshold() == 0.3
    with pytest.raises(ConfigurationError, match="API_PUBLIC_URL"):
        dependencies.get_api_url()


def test_no_config_is_fine_until_needed(api_env):
    assert dependencies.get_api_path() == ""
    assert dependencies.get_api_root_path() == ""
    assert dependencies.get_trgm_search_threshold() == 0.3
    with pytest.raises(ConfigurationError, match="API_PUBLIC_URL"):
        dependencies.get_api_url()


def test_cursor_signing_key_setting_is_used(monkeypatch):
    from app.utils import cursor_utils

    monkeypatch.delenv("CRUCIBLE_CURSOR_SIGNING_KEYS", raising=False)
    monkeypatch.setenv("CRUCIBLE_SECRET_KEY", "main-secret" * 3)
    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEY", "cursor-secret" * 3)
    assert cursor_utils._signing_keys() == ("v1", {"v1": b"cursor-secret" * 3})
    monkeypatch.delenv("CRUCIBLE_CURSOR_SIGNING_KEY")
    get_settings.cache_clear()
    assert cursor_utils._signing_keys() == ("v1", {"v1": b"main-secret" * 3})


@pytest.mark.parametrize("name", ["CRUCIBLE_SECRET_KEY", "CRUCIBLE_CURSOR_SIGNING_KEY"])
def test_signing_keys_shorter_than_32_characters_are_refused(monkeypatch, name):
    from app.config import ConfigurationError, get_secret

    monkeypatch.setenv(name, "x" * 31)
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match=f"{name} must be at least 32 characters"):
        get_secret(name)
    monkeypatch.setenv(name, "x" * 32)
    get_settings.cache_clear()
    assert get_secret(name) == "x" * 32


def test_import_needs_no_settings(tmp_path):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(REPO), "CRUCIBLE_SECRETS_DIR": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", "import app.api.main"],
        cwd=tmp_path, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("address,warned", [
    ("http://localhost:8080", False), ("http://127.0.0.1:8080", False), ("https://crucible.mylab.org", False),
    ("http://crucible.mylab.org", True), ("http://192.168.1.20:8080", True),
])
def test_a_plain_http_public_address_off_localhost_is_warned_about(monkeypatch, caplog, address, warned):
    import logging

    from app import config

    monkeypatch.setenv("API_PUBLIC_URL", address)
    config.get_settings.cache_clear()
    with caplog.at_level(logging.WARNING, logger="app.config"):
        config._warn_about_plain_http(config.get_settings())
    assert ("plain HTTP" in caplog.text) is warned
