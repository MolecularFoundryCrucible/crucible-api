from pathlib import Path
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException
from fastapi.security import HTTPBasicCredentials
from google.api_core.exceptions import PermissionDenied
from google.auth.exceptions import DefaultCredentialsError
import pytest
import yaml

from app.api.routes.hub_auth import require_hub_client
from app.utils import hub_secrets
from app.utils.hub_login import HubLoginUnavailable, load_login_config
from app.utils.hub_orcid import _orcid_client_secret


VERSION = "projects/test-project/secrets/hub-client/versions/1"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch):
    for name in ("HUB_AUTH_ENABLED", "HUB_AUTH_CLIENT_SECRET", "HUB_AUTH_CLIENT_SECRET_VERSION",
                 "HUB_AUTH_ORCID_CLIENT_SECRET", "HUB_AUTH_ORCID_CLIENT_SECRET_VERSION"):
        monkeypatch.delenv(name, raising=False)
    hub_secrets._read_secret_version.cache_clear()
    yield
    hub_secrets._read_secret_version.cache_clear()


@pytest.fixture
def manager(monkeypatch):
    factory = MagicMock()
    factory.return_value.__enter__.return_value.access_secret_version.return_value = SimpleNamespace(
        payload=SimpleNamespace(data=b"test-secret-value"),
    )
    monkeypatch.setattr(hub_secrets.secretmanager, "SecretManagerServiceClient", factory)
    return factory


def test_local_env_needs_no_cloud_access(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "local-secret")
    assert hub_secrets.get_hub_secret("HUB_AUTH_CLIENT_SECRET") == "local-secret"
    manager.assert_not_called()


def test_version_precedence_cache_and_rotation(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "ignored-local-value")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    for _ in range(2):
        assert hub_secrets.get_hub_secret("HUB_AUTH_CLIENT_SECRET") == "test-secret-value"
    client = manager.return_value.__enter__.return_value
    client.access_secret_version.assert_called_once_with(request={"name": VERSION}, timeout=5, retry=None)
    assert os.environ["HUB_AUTH_CLIENT_SECRET"] == "ignored-local-value"
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION[:-1] + "2")
    hub_secrets.get_hub_secret("HUB_AUTH_CLIENT_SECRET")
    assert client.access_secret_version.call_count == 2


@pytest.mark.parametrize("version", ["bad", VERSION.replace("/1", "/latest"), VERSION.replace("/1", "/0")])
def test_references_require_pinned_versions(monkeypatch, manager, version):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", version)
    with pytest.raises(hub_secrets.HubSecretUnavailable):
        hub_secrets.get_hub_secret("HUB_AUTH_CLIENT_SECRET")
    manager.assert_not_called()


@pytest.mark.parametrize("error", [PermissionDenied("sensitive-error"), DefaultCredentialsError("sensitive-error")])
def test_cloud_failure_is_not_cached_and_never_falls_back(monkeypatch, manager, error):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET", "must-not-fallback")
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    client = manager.return_value.__enter__.return_value
    client.access_secret_version.side_effect = error
    with pytest.raises(hub_secrets.HubSecretUnavailable) as result:
        hub_secrets.get_hub_secret("HUB_AUTH_CLIENT_SECRET")
    assert "sensitive-error" not in str(result.value)
    client.access_secret_version.side_effect = None
    assert hub_secrets.get_hub_secret("HUB_AUTH_CLIENT_SECRET") == "test-secret-value"
    assert client.access_secret_version.call_count == 2


@pytest.mark.parametrize("payload", [b"", b" ", b"secret\n", b"secret\r", b"\xff"])
def test_invalid_payloads_are_rejected(monkeypatch, manager, payload):
    monkeypatch.setenv("HUB_AUTH_CLIENT_SECRET_VERSION", VERSION)
    manager.return_value.__enter__.return_value.access_secret_version.return_value.payload.data = payload
    with pytest.raises(hub_secrets.HubSecretUnavailable):
        hub_secrets.get_hub_secret("HUB_AUTH_CLIENT_SECRET")


def test_hub_auth_is_disabled_when_unset():
    assert hub_secrets.hub_auth_enabled() is False


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


def test_orcid_reference_failure_does_not_use_legacy_secret(monkeypatch, manager):
    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_SECRET_VERSION", VERSION)
    monkeypatch.setenv("HUB_AUTH_ORCID_CLIENT_SECRET", "ignored-inline-secret")
    manager.side_effect = DefaultCredentialsError("sensitive-error")
    with pytest.raises(HubLoginUnavailable):
        _orcid_client_secret()


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_deployment_references_only_and_enabled_only_on_staging(environment):
    config = yaml.safe_load((ROOT / f"k8s-conf/configmap-{environment}.yaml").read_text())["data"]
    deployment = yaml.safe_load((ROOT / f"k8s-conf/deployment-k8s-conf-{environment}.yaml").read_text())
    values = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert config["HUB_AUTH_ENABLED"] == ("true" if environment == "staging" else "false")
    for key in config:
        if key.startswith("HUB_AUTH_"):
            assert values[key]["valueFrom"]["configMapKeyRef"] == {"name": f"crucible-api-config-{environment}", "key": key}
    for key in ("HUB_AUTH_CLIENT_SECRET", "HUB_AUTH_ORCID_CLIENT_SECRET"):
        assert key not in config and key not in values
        assert f"crucible-hub-{environment}-" in config[key + "_VERSION"]
        assert config[key + "_VERSION"].rsplit("/", 1)[1].isdigit()
    if environment == "staging":
        assert config["HUB_AUTH_CALLBACK_URI"] == "http://localhost:3000/hub/auth/callback"
    else:
        assert config["HUB_AUTH_CALLBACK_URI"] == "https://crucible.lbl.gov/hub/auth/callback"
