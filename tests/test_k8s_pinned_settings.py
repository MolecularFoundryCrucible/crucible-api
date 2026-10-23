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
