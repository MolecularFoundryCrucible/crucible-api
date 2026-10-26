from pathlib import Path

import pytest
import yaml

K8S = Path(__file__).resolve().parent.parent / "k8s-conf"
SECRETS = "projects/776258882599/secrets"
PINNED = {
    "PGDB_PW_VERSION": f"{SECRETS}/pgcloud_pw/versions/2",
    "CRUCIBLE_SECRET_KEY_VERSION": f"{SECRETS}/crucible_secret_key/versions/1",
    "RABBITMQ_DEFAULT_PW_VERSION": f"{SECRETS}/rabbitmq_default_pw/versions/1",
    "GCS_SA_VERSION": f"{SECRETS}/service-account-cred/versions/latest",
    "MQTT_PASSWORD_VERSION": f"{SECRETS}/mqtt-crucible-printers-passwd/versions/1",
    "PROPDB_APIKEY_VERSION": f"{SECRETS}/propdb-apikey/versions/1",
    "RMQ_HOST": "10.128.0.31",
    "RMQ_PORT": "5672",
    "RMQ_USER": "admin",
    "GCS_PROJECT": "crucible-prod",
    "MQTT_HOST": "mqtt.mfdata.org",
    "MQTT_PORT": "8883",
    "MQTT_USERNAME": "crucible-printers",
    "PROPOSAL_DB_URL": "https://foundry-admin.lbl.gov/api/JSON/sciCat-GetUser.aspx",
    "PRINTING_ENABLED": "true",
    "PROPOSAL_SYNC_ENABLED": "true",
    "INGESTION_ENABLED": "true",
}
PORTS = {"main": "7777", "staging": "8889"}


def _load(name):
    return yaml.safe_load((K8S / name).read_text())


def _configmap_refs(container):
    return {
        entry["name"]: entry["valueFrom"]["configMapKeyRef"]
        for entry in container.get("env", [])
        if "configMapKeyRef" in entry.get("valueFrom", {})
    }


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_configmap_pins_current_defaults(environment):
    data = _load(f"configmap-{environment}.yaml")["data"]
    for key, value in PINNED.items() | {("PORT", PORTS[environment])}:
        assert data.get(key) == value, key


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_api_container_receives_every_pinned_key(environment):
    spec = _load(f"deployment-k8s-conf-{environment}.yaml")["spec"]["template"]["spec"]
    container = next(c for c in spec["containers"] if c["name"] == "crucible-api-container")
    refs = _configmap_refs(container)
    for key in [*PINNED, "PORT"]:
        assert refs.get(key) == {"key": key, "name": f"crucible-api-config-{environment}"}, key


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_cleanup_job_receives_database_password_reference(environment):
    job = _load(f"hub-auth-cleanup-{environment}.yaml")
    container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    refs = _configmap_refs(container)
    assert refs.get("PGDB_PW_VERSION") == {"key": "PGDB_PW_VERSION", "name": f"crucible-api-config-{environment}"}


OUTSIDE_SETTINGS = {
    "ANTHROPIC_BASE_URL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "DB_AUTH", "DB_TYPE", "PORT", "PYTHONUNBUFFERED",
    "MULTIPART_UPLOAD_BUCKET", "MULTIPART_UPLOAD_ENABLED", "MULTIPART_UPLOAD_PROJECT", "MULTIPART_UPLOAD_SIGNER",
}


def _api_environment(environment):
    data = _load(f"configmap-{environment}.yaml")["data"]
    spec = _load(f"deployment-k8s-conf-{environment}.yaml")["spec"]["template"]["spec"]
    container = next(c for c in spec["containers"] if c["name"] == "crucible-api-container")
    values = {}
    for entry in container.get("env", []):
        if "value" in entry:
            values[entry["name"]] = str(entry["value"])
        elif "configMapKeyRef" in entry.get("valueFrom", {}):
            values[entry["name"]] = str(data[entry["valueFrom"]["configMapKeyRef"]["key"]])
    return data, values


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_foundry_settings_resolve_and_pass_the_startup_check(environment, monkeypatch, tmp_path):
    from app import config

    known = {group.env_name(field) for group in config.GROUPS.values() for field in group.model_fields}
    versions = {f"{name}_VERSION" for name in config._SECRETS}
    for name in known | versions | OUTSIDE_SETTINGS | {"DB_CONF_FILE"}:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    data, values = _api_environment(environment)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()

    assert set(values) - known - versions - OUTSIDE_SETTINGS == set()
    config.check_settings()
    settings = config.get_settings()
    assert (settings.db.server, settings.db.username, settings.db.database) == (data["db_server"], data["db_username"], data["db_database"])
    assert settings.db.expected_revisions == data["EXPECTED_DB_REVISIONS"]
    assert settings.hub.enabled is (data["HUB_AUTH_ENABLED"] == "true")
    assert (settings.hub.client_id, settings.hub.callback_uri) == (data["HUB_AUTH_CLIENT_ID"], data["HUB_AUTH_CALLBACK_URI"])
    assert settings.storage.upload_bucket == data["UPLOAD_BUCKET"]
    assert (settings.ingestion.enabled, settings.ingestion.host, settings.ingestion.port) == (True, data["RMQ_HOST"], int(data["RMQ_PORT"]))
    assert (settings.printing.enabled, settings.printing.port) == (True, int(data["MQTT_PORT"]))
    assert (settings.proposals.enabled, settings.proposals.url) == (True, data["PROPOSAL_DB_URL"])


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_cleanup_job_settings_resolve(environment, monkeypatch, tmp_path):
    from app import config

    data = _load(f"configmap-{environment}.yaml")["data"]
    job = _load(f"hub-auth-cleanup-{environment}.yaml")
    container = job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]
    known = {group.env_name(field) for group in config.GROUPS.values() for field in group.model_fields}
    for name in known | {f"{name}_VERSION" for name in config._SECRETS} | {"DB_CONF_FILE"}:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    for entry in container["env"]:
        value = entry["value"] if "value" in entry else data[entry["valueFrom"]["configMapKeyRef"]["key"]]
        monkeypatch.setenv(entry["name"], str(value))
    config.get_settings.cache_clear()
    db = config.get_settings().db
    assert (db.server, db.pool_size, db.max_overflow) == (data["db_server"], 1, 0)
    assert config.get_settings().db.missing("server", "username", "database", "password") == []
