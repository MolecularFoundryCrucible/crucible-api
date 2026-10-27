from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from google.api_core.exceptions import PermissionDenied
from google.auth.exceptions import DefaultCredentialsError
import pytest

from app.config import ConfigurationError
import yaml

from app.api.routes.hub_auth import require_hub_client
from app.config import optional_secret
from app.utils.hub_login import HubLoginUnavailable, hub_auth_enabled, load_login_config
from app.utils.hub_orcid import _orcid_client_secret


VERSION = "projects/test-project/secrets/hub-client/versions/1"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    for name in ("HUB_AUTH_ENABLED", "HUB_AUTH_CLIENT_SECRET", "HUB_AUTH_CLIENT_SECRET_VERSION",
                 "HUB_AUTH_ORCID_CLIENT_SECRET", "HUB_AUTH_ORCID_CLIENT_SECRET_VERSION"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def manager(monkeypatch):
    factory = MagicMock()
    factory.return_value.__enter__.return_value.access_secret_version.return_value = SimpleNamespace(
        payload=SimpleNamespace(data=b"test-secret-value"),
    )
    from google.cloud import secretmanager

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", factory)
    return factory


def test_local_env_needs_no_cloud_access(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "local-secret")
    assert optional_secret("HUB_AUTH_CLIENT_SECRET") == "local-secret"
    manager.assert_not_called()


def test_version_cache_and_rotation(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    for _ in range(2):
        assert optional_secret("HUB_AUTH_CLIENT_SECRET") == "test-secret-value"
    client = manager.return_value.__enter__.return_value
    client.access_secret_version.assert_called_once_with(request={"name": VERSION}, timeout=10, retry=None)
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION[:-1] + "2")
    optional_secret("HUB_AUTH_CLIENT_SECRET")
    assert client.access_secret_version.call_count == 2
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION.replace("/1", "/latest"))
    assert optional_secret("HUB_AUTH_CLIENT_SECRET") == "test-secret-value"
    assert client.access_secret_version.call_count == 3


@pytest.mark.parametrize("version", ["bad", VERSION.replace("/1", "/0"), VERSION.replace("/1", "/latest2")])
def test_references_reject_invalid_versions(monkeypatch, manager, version):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", version)
    with pytest.raises(ConfigurationError, match="HUB_AUTH_CLIENT_SECRET_VERSION must be a full"):
        optional_secret("HUB_AUTH_CLIENT_SECRET")
    manager.assert_not_called()


@pytest.mark.parametrize("error", [PermissionDenied("sensitive-error"), DefaultCredentialsError("sensitive-error")])
def test_cloud_failure_is_not_cached(monkeypatch, manager, error):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    client = manager.return_value.__enter__.return_value
    client.access_secret_version.side_effect = error
    with pytest.raises(ConfigurationError) as result:
        optional_secret("HUB_AUTH_CLIENT_SECRET")
    assert str(result.value) == f"HUB_AUTH_CLIENT_SECRET_VERSION could not be read: {error.__class__.__name__}"
    assert result.value.__cause__ is None and result.value.__suppress_context__
    client.access_secret_version.side_effect = None
    assert optional_secret("HUB_AUTH_CLIENT_SECRET") == "test-secret-value"
    assert client.access_secret_version.call_count == 2


@pytest.mark.parametrize("payload", [b"", b" ", b"secret\n", b"secret\r", b"sec\tret", b"\xff", "s\u00e9cret".encode()])
def test_invalid_payloads_are_rejected(monkeypatch, manager, payload):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    manager.return_value.__enter__.return_value.access_secret_version.return_value.payload.data = payload
    with pytest.raises(ConfigurationError):
        optional_secret("HUB_AUTH_CLIENT_SECRET")


def test_hub_secrets_from_files_follow_the_same_rule(monkeypatch, manager, tmp_path):
    (tmp_path / "HUB_AUTH_CLIENT_SECRET").write_text("first line\nsecond line\n")
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(tmp_path))
    with pytest.raises(ConfigurationError, match="HUB_AUTH_CLIENT_SECRET must be one line of printable ASCII"):
        optional_secret("HUB_AUTH_CLIENT_SECRET")


def test_other_secrets_keep_their_value_as_given(monkeypatch, manager):
    monkeypatch.delenv("ORCID_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("ORCID_CLIENT_SECRET_VERSION", VERSION)
    manager.return_value.__enter__.return_value.access_secret_version.return_value.payload.data = b"value\n"
    assert optional_secret("ORCID_CLIENT_SECRET") == "value\n"


def test_hub_auth_is_disabled_when_unset():
    assert hub_auth_enabled() is False


def test_managed_secret_authenticates_client(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    monkeypatch.setenv("HUB_AUTH_CLIENT_ID", "crucible-hub")
    assert require_hub_client(HTTPBasicCredentials(username="crucible-hub", password="test-secret-value")) == "crucible-hub"


def test_disabled_auth_never_fetches_secrets(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_ENABLED", "false")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    with pytest.raises(HTTPException) as result:
        require_hub_client(None)
    assert result.value.status_code == 503
    with pytest.raises(HubLoginUnavailable):
        load_login_config()
    manager.assert_not_called()


def test_unavailable_reference_becomes_safe_auth_failure(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "https://example.test/hub/auth/callback")
    monkeypatch.setenv("HUB_AUTH_PUBLIC_API_URL", "https://example.test/api/v3")
    manager.side_effect = DefaultCredentialsError("sensitive-error")
    with pytest.raises(HTTPException) as result:
        require_hub_client(None)
    assert result.value.status_code == 503
    assert "sensitive-error" not in str(result.value)
    with pytest.raises(HubLoginUnavailable):
        load_login_config()
    assert manager.call_count == 2


def test_orcid_reference_failure_does_not_use_legacy_secret(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_SECRET_VERSION", VERSION)
    monkeypatch.setenv("ORCID_CLIENT_SECRET", "legacy-secret")
    manager.side_effect = DefaultCredentialsError("sensitive-error")
    with pytest.raises(HubLoginUnavailable):
        _orcid_client_secret()
    manager.assert_called_once()


def test_hub_secret_from_the_secrets_directory(monkeypatch, manager, tmp_path):
    (tmp_path / "HUB_AUTH_CLIENT_SECRET").write_text("file-secret\n")
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(tmp_path))
    assert optional_secret("HUB_AUTH_CLIENT_SECRET") == "file-secret"
    manager.assert_not_called()


def test_hub_secret_given_two_ways_is_refused(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "inline")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    with pytest.raises(ConfigurationError, match="HUB_AUTH_CLIENT_SECRET is given more than one way"):
        optional_secret("HUB_AUTH_CLIENT_SECRET")
    manager.assert_not_called()


def test_startup_check_requires_hub_secrets_when_enabled(monkeypatch):
    from app.config import missing_required_settings

    for name in ("HUB_AUTH_CALLBACK_URI", "ORCID_CLIENT_SECRET", "ORCID_CLIENT_SECRET_VERSION"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    missing = missing_required_settings()
    assert {"HUB_AUTH_CLIENT_SECRET", "HUB_AUTH_CALLBACK_URI", "HUB_AUTH_ORCID_CLIENT_SECRET or ORCID_CLIENT_SECRET"} <= set(missing)


def test_startup_check_accepts_hub_secret_references(monkeypatch):
    from app.config import get_settings, missing_required_settings

    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_CALLBACK_URI", "https://example.test/hub/auth/callback")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    monkeypatch.setenv("ORCID_CLIENT_SECRET", "api-orcid-secret")
    get_settings.cache_clear()
    assert not [name for name in missing_required_settings() if name.startswith("HUB_AUTH")]


def test_importing_the_application_does_not_load_secret_manager():
    import subprocess
    import sys

    code = "import sys, app.api.main; print('google.cloud.secretmanager' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_deployment_references_only_and_enabled(environment):
    config = yaml.safe_load((ROOT / f"k8s-conf/configmap-{environment}.yaml").read_text())["data"]
    deployment = yaml.safe_load((ROOT / f"k8s-conf/deployment-k8s-conf-{environment}.yaml").read_text())
    values = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert config["HUB_AUTH_ENABLED"] == "true"
    for key in config:
        if key.startswith("HUB_AUTH_"):
            assert values[key]["valueFrom"]["configMapKeyRef"] == {"name": f"crucible-api-config-{environment}", "key": key}
    expected_versions = {
        "HUB_AUTH_CLIENT_SECRET": "projects/776258882599/secrets/crucible-hub-client-secret/versions/latest",
        "HUB_AUTH_ORCID_CLIENT_SECRET": "projects/776258882599/secrets/crucible-hub-orcid-client-secret/versions/latest",
    }
    for key, expected_version in expected_versions.items():
        assert key not in config and key not in values
        assert config[key + "_VERSION"] == expected_version
    for key in ("ORCID_CLIENT_ID", "ORCID_CLIENT_SECRET_VERSION"):
        assert values[key]["valueFrom"]["configMapKeyRef"] == {"name": f"crucible-api-config-{environment}", "key": key}
    assert "ORCID_CLIENT_SECRET" not in config and "ORCID_CLIENT_SECRET" not in values
    assert config["ORCID_CLIENT_SECRET_VERSION"] == "projects/776258882599/secrets/orcid_client_secret_esb/versions/latest"
    if environment == "staging":
        assert config["HUB_AUTH_CALLBACK_URI"].split(",")[0] == "http://localhost:3000/hub/auth/callback"
    else:
        assert config["HUB_AUTH_CALLBACK_URI"] == "https://crucible.lbl.gov/hub/auth/callback"


def test_direct_api_orcid_config_and_secret_version(monkeypatch, tmp_path):
    from app.api import dependencies
    import app.config as config

    monkeypatch.setattr(dependencies, "get_api_config", lambda: {"orcid_client_id": "fallback-client-id"})
    monkeypatch.delenv("ORCID_CLIENT_ID", raising=False)
    assert dependencies.get_orcid_client_id() == "fallback-client-id"

    monkeypatch.setenv("ORCID_CLIENT_ID", "env-client-id")
    config.get_settings.cache_clear()
    assert dependencies.get_orcid_client_id() == "env-client-id"

    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ORCID_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("ORCID_CLIENT_SECRET_VERSION", raising=False)
    with pytest.raises(RuntimeError):
        dependencies.get_orcid_client_secret()

    captured = {}

    def fake_get_gcs_secret(secret_name):
        captured["secret_name"] = secret_name
        return "fetched-orcid-secret"

    monkeypatch.setattr(config, "get_gcs_secret", fake_get_gcs_secret)
    version_path = "projects/776258882599/secrets/orcid_client_secret/versions/2"
    monkeypatch.setenv("ORCID_CLIENT_SECRET_VERSION", version_path)
    assert dependencies.get_orcid_client_secret() == "fetched-orcid-secret"
    assert captured["secret_name"] == version_path

    monkeypatch.delenv("ORCID_CLIENT_SECRET", raising=False)
    monkeypatch.setenv("ORCID_CLIENT_SECRET_VERSION", "orcid_client_secret_esb/versions/latest")
    with pytest.raises(ConfigurationError, match="ORCID_CLIENT_SECRET_VERSION"):
        dependencies.get_orcid_client_secret()


def test_hub_orcid_secret_missing_is_unavailable(monkeypatch, tmp_path):
    from app import config
    from app.utils import hub_orcid

    monkeypatch.delenv("CRUCIBLE_SECRETS_DIR", raising=False)
    monkeypatch.setattr(config, "DEFAULT_SECRETS_DIR", str(tmp_path / "no-secrets"))
    from app.utils.hub_login import HubLoginUnavailable

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DB_CONF_FILE", raising=False)
    for name in ("HUB_AUTH_ORCID_CLIENT_SECRET", "ORCID_CLIENT_SECRET"):
        for suffix in ("", "_VERSION"):
            monkeypatch.delenv(f"{name}{suffix}", raising=False)
    with pytest.raises(HubLoginUnavailable):
        hub_orcid._orcid_client_secret()


