import io
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import yaml

from app import config
from app.cli import settings as check
from app.cli.__main__ import main

K8S = Path(__file__).resolve().parent.parent / "k8s-conf"
VERSION = "projects/1/secrets/{}/versions/1"
SERVER_ENV = {
    "DB_SERVER": "db:5432", "DB_USERNAME": "crucible", "DB_DATABASE": "crucible",
    "PGDB_PW_VERSION": VERSION.format("pgdb"), "CRUCIBLE_SECRET_KEY_VERSION": VERSION.format("key"),
}


def configmap(data, name="settings"):
    return {"kind": "ConfigMap", "metadata": {"name": name}, "data": data}


def deployment(env=(), env_from=(), mounts=(), volumes=(), name="api"):
    container = {"name": name, "env": list(env), "envFrom": list(env_from), "volumeMounts": list(mounts)}
    return {"kind": "Deployment", "metadata": {"name": "crucible-api"}, "spec": {"template": {"spec": {"containers": [container], "volumes": list(volumes)}}}}


def from_configmap(names, configmap_name="settings"):
    return [{"name": name, "valueFrom": {"configMapKeyRef": {"name": configmap_name, "key": name}}} for name in names]


def server_documents(extra=None, **kwargs):
    data = {**SERVER_ENV, **(extra or {})}
    return [configmap(data), deployment(env=from_configmap(data), **kwargs)]


def run(documents, servers=("api",), databases=()):
    [(_, _, report)] = check.check_documents(documents, list(servers), list(databases))
    return report


@pytest.mark.parametrize("environment", ["main", "staging"])
def test_foundry_manifests_pass(environment):
    documents = check.load_documents([str(K8S / f"configmap-{environment}.yaml"), str(K8S / f"deployment-k8s-conf-{environment}.yaml"), str(K8S / f"hub-auth-cleanup-{environment}.yaml")])
    results = check.check_documents(documents, ["crucible-api-container"], ["cleanup"])
    assert [(container.name, profile, report.errors) for container, profile, report in results] == [
        ("crucible-api-container", "server", []), ("cleanup", "database", []),
    ]
    assert results[0][2].warnings == [f"{name} is not a Crucible setting" for name in ("ANTHROPIC_BASE_URL", "ANTHROPIC_DEFAULT_HAIKU_MODEL", "DB_AUTH", "DB_TYPE")] + [
        "GCS_SA_VERSION is no longer read (Google storage uses Application Default Credentials); remove it"]


def test_a_complete_server_passes_and_the_process_is_left_alone():
    before = dict(os.environ)
    assert run(server_documents()).errors == []
    assert dict(os.environ) == before
    assert config.get_settings.cache_info().currsize == 0


def test_missing_required_setting_fails():
    documents = server_documents()
    del documents[0]["data"]["CRUCIBLE_SECRET_KEY_VERSION"]
    documents[1] = deployment(env=from_configmap(documents[0]["data"]))
    assert run(documents).errors == ["Missing required settings: CRUCIBLE_SECRET_KEY"]


def test_setting_given_two_ways_fails():
    documents = server_documents()
    documents[1]["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "CRUCIBLE_SECRET_KEY", "value": "inline"})
    assert run(documents).errors == ["CRUCIBLE_SECRET_KEY is given more than one way (CRUCIBLE_SECRET_KEY, CRUCIBLE_SECRET_KEY_VERSION); give it one way only"]


def test_malformed_secret_manager_path_fails():
    report = run(server_documents({"CRUCIBLE_SECRET_KEY_VERSION": "crucible_secret_key/versions/1"}))
    assert report.errors == [
        "CRUCIBLE_SECRET_KEY_VERSION must be a full projects/<project>/secrets/<secret>/versions/<number or latest> path",
        "Missing required settings: CRUCIBLE_SECRET_KEY",
    ]


def test_invalid_value_fails():
    assert run(server_documents({"DB_POOL_SIZE": "many"})).errors[0].startswith("Invalid settings: DB_POOL_SIZE")


def test_feature_settings_are_required_only_when_the_feature_is_on():
    assert run(server_documents({"PRINTING_ENABLED": "true"})).errors == ["Missing required settings: MQTT_HOST, MQTT_PASSWORD, MQTT_USERNAME"]


def test_missing_configmap_key_fails_unless_optional():
    documents = server_documents()
    env = documents[1]["spec"]["template"]["spec"]["containers"][0]["env"]
    env.append({"name": "RMQ_HOST", "valueFrom": {"configMapKeyRef": {"name": "settings", "key": "absent"}}})
    assert run(documents).errors[0] == "RMQ_HOST refers to key absent of ConfigMap settings, which the input does not have"
    env[-1]["valueFrom"]["configMapKeyRef"]["optional"] = True
    assert run(documents).errors == []


def test_env_from_configmap_and_kubernetes_secret_values():
    documents = [
        configmap({key: value for key, value in SERVER_ENV.items() if key != "CRUCIBLE_SECRET_KEY_VERSION"}),
        deployment(
            env_from=[{"configMapRef": {"name": "settings"}}],
            env=[{"name": "CRUCIBLE_SECRET_KEY", "valueFrom": {"secretKeyRef": {"name": "keys", "key": "secret"}}}],
        ),
    ]
    assert run(documents).errors == []


def test_secret_files_mounted_by_the_gke_secret_manager_add_on():
    provider = {
        "kind": "SecretProviderClass", "metadata": {"name": "crucible-secrets"},
        "spec": {"provider": "gke", "parameters": {"secrets": yaml.safe_dump([
            {"resourceName": VERSION.format("pgdb"), "path": "PGDB_PW"},
            {"resourceName": VERSION.format("key"), "path": "CRUCIBLE_SECRET_KEY"},
        ])}},
    }
    volume = {"name": "secrets", "csi": {"driver": check.GKE_SECRET_MANAGER_DRIVER, "readOnly": True, "volumeAttributes": {"secretProviderClass": "crucible-secrets"}}}
    data = {key: value for key, value in SERVER_ENV.items() if not key.endswith("_VERSION")}
    documents = [configmap(data), provider, deployment(env=from_configmap(data), mounts=[{"name": "secrets", "mountPath": "/run/secrets"}], volumes=[volume])]
    [(container, _, report)] = check.check_documents(documents, ["api"], [])
    assert report.errors == []
    assert sorted(container.secret_manager_paths) == sorted([VERSION.format("pgdb"), VERSION.format("key")])

    data["PGDB_PW_VERSION"] = VERSION.format("pgdb")
    documents[0] = configmap(data)
    documents[2] = deployment(env=from_configmap(data), mounts=[{"name": "secrets", "mountPath": "/run/secrets"}], volumes=[volume])
    assert run(documents).errors == ["PGDB_PW is given more than one way (a PGDB_PW file in the secrets directory, PGDB_PW_VERSION); give it one way only"]


def test_secret_volume_elsewhere_is_not_the_secrets_directory():
    volume = {"name": "keys", "secret": {"secretName": "keys", "items": [{"key": "pw", "path": "PGDB_PW"}]}}
    data = {key: value for key, value in SERVER_ENV.items() if key != "PGDB_PW_VERSION"}
    documents = [configmap(data), deployment(env=from_configmap(data), mounts=[{"name": "keys", "mountPath": "/etc/keys"}], volumes=[volume])]
    assert run(documents).errors == ["Missing required settings: PGDB_PW"]
    documents[1]["spec"]["template"]["spec"]["containers"][0]["env"].append({"name": "PGDB_PW_FILE", "value": "/etc/keys/PGDB_PW"})
    assert run(documents).errors == []


def test_file_pointer_to_a_path_no_volume_provides_fails():
    documents = server_documents()
    del documents[0]["data"]["PGDB_PW_VERSION"]
    documents[1] = deployment(env=from_configmap(documents[0]["data"]) + [{"name": "PGDB_PW_FILE", "value": "/missing/pw"}])
    assert "PGDB_PW_FILE points to /missing/pw, which no volume in the pod provides" in run(documents).errors


def test_unknown_variables_are_warnings():
    report = run(server_documents({"RMQ_HOTS": "broker", "PORT": "8000"}))
    assert report.errors == []
    assert report.warnings == ["RMQ_HOTS is not a Crucible setting"]


def test_database_profile_checks_database_settings_only():
    data = {key: value for key, value in SERVER_ENV.items() if key != "CRUCIBLE_SECRET_KEY_VERSION"}
    documents = [configmap(data), deployment(env=from_configmap(data), name="cleanup")]
    assert run(documents, servers=(), databases=("cleanup",)).errors == []
    del documents[0]["data"]["PGDB_PW_VERSION"]
    documents[1] = deployment(env=from_configmap(documents[0]["data"]), name="cleanup")
    assert run(documents, servers=(), databases=("cleanup",)).errors == ["Missing required settings: PGDB_PW"]


def test_unknown_container_is_an_error():
    assert run(server_documents(), servers=("nope",)).errors == ["no container named nope in the input (found: api)"]


def test_verify_secrets_reads_metadata_only(monkeypatch):
    from google.cloud import secretmanager

    factory = MagicMock()
    client = factory.return_value.__enter__.return_value
    states = {VERSION.format("pgdb"): "ENABLED", VERSION.format("key"): "DISABLED"}
    client.get_secret_version.side_effect = lambda request, timeout: SimpleNamespace(state=SimpleNamespace(name=states[request["name"]]))
    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", factory)
    [(_, _, report)] = check.check_documents(server_documents(), ["api"], [], verify_secrets=True)
    assert report.errors == [f"{VERSION.format('key')}: version is DISABLED"]
    client.access_secret_version.assert_not_called()


def test_verify_secrets_reports_missing_versions(monkeypatch):
    from google.api_core.exceptions import NotFound
    from google.cloud import secretmanager

    factory = MagicMock()
    factory.return_value.__enter__.return_value.get_secret_version.side_effect = NotFound("secret value must not appear")
    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", factory)
    [(_, _, report)] = check.check_documents(server_documents(), ["api"], [], verify_secrets=True)
    assert report.errors == [f"{VERSION.format('key')}: NotFound", f"{VERSION.format('pgdb')}: NotFound"]


def test_command_exit_codes_and_standard_input(tmp_path, monkeypatch, capsys):
    good = tmp_path / "good.yaml"
    good.write_text(yaml.safe_dump_all(server_documents()))
    assert main(["settings", "check", str(good), "--server", "api"]) == 0
    assert "Deployment/crucible-api api (server): ok" in capsys.readouterr().out

    bad = server_documents()
    del bad[0]["data"]["DB_SERVER"]
    monkeypatch.setattr("sys.stdin", io.StringIO(yaml.safe_dump({"kind": "List", "items": bad})))
    assert main(["settings", "check", "-", "--server", "api"]) == 1
    assert "error: Missing required settings: DB_SERVER" in capsys.readouterr().out

    assert main(["settings", "check", str(good)]) == 2
