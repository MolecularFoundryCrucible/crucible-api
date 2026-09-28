import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("environment", ["staging", "main"])
def test_cleanup_manifest(environment):
    manifest = yaml.safe_load((ROOT / f"k8s-conf/hub-auth-cleanup-{environment}.yaml").read_text())
    deployment = yaml.safe_load((ROOT / f"k8s-conf/deployment-k8s-conf-{environment}.yaml").read_text())
    spec = manifest["spec"]
    assert manifest["kind"] == "CronJob"
    assert spec["suspend"] is True
    assert spec["concurrencyPolicy"] == "Forbid"
    assert spec["timeZone"] == "Etc/UTC"
    assert spec["schedule"].split()[1:] == ["*"] * 4
    job = spec["jobTemplate"]["spec"]
    assert job["backoffLimit"] == 1
    assert job["activeDeadlineSeconds"] == 600
    pod = job["template"]["spec"]
    api_pod = deployment["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    assert pod["serviceAccountName"] == api_pod["serviceAccountName"]
    assert "volumes" not in pod
    container, = pod["containers"]
    api_container, = api_pod["containers"]
    assert container["image"] == api_container["image"]
    assert container["command"] == ["/root/.venv/bin/python", "/root/scripts/cleanup_hub_auth.py"]
    assert container["args"] == ["--commit", "--batch-size", "1000", "--batches", "10"]
    api_env = {entry["name"]: entry for entry in api_container["env"]}
    env = {entry["name"]: entry for entry in container["env"]}
    for name in ("API_CONF_FILE", "DB_SERVER", "DB_USERNAME", "DB_DATABASE"):
        assert env[name] == api_env[name]
    assert env["DB_POOL_SIZE"]["value"] == "1"
    assert env["DB_MAX_OVERFLOW"]["value"] == "0"
    assert "COPY scripts/cleanup_hub_auth.py /root/scripts/cleanup_hub_auth.py" in (ROOT / "Dockerfile").read_text()


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("cleanup_script", ROOT / "scripts/cleanup_hub_auth.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("commit", [False, True])
def test_cleanup_cli_batch_limit(script, monkeypatch, capsys, commit):
    engine = Mock()
    cleanup = Mock(return_value={"hubaccesstoken": 1})
    monkeypatch.setattr(script, "setup_engine", Mock(return_value=engine))
    monkeypatch.setattr(script, "cleanup_hub_auth", cleanup)
    monkeypatch.setattr("sys.argv", ["cleanup", "--batches", "2"] + (["--commit"] if commit else []))
    script.main()
    assert cleanup.call_count == (2 if commit else 1)
    cleanup.assert_called_with(engine, commit=commit, batch_size=1000)
    engine.dispose.assert_called_once()
    assert json.loads(capsys.readouterr().out.splitlines()[0])["mode"] == ("commit" if commit else "dry-run")


def test_cleanup_cli_stops_on_empty_batch(script, monkeypatch):
    engine = Mock()
    cleanup = Mock(return_value={"hubaccesstoken": 0})
    monkeypatch.setattr(script, "setup_engine", Mock(return_value=engine))
    monkeypatch.setattr(script, "cleanup_hub_auth", cleanup)
    monkeypatch.setattr("sys.argv", ["cleanup", "--commit", "--batches", "10"])
    script.main()
    assert cleanup.call_count == 1


@pytest.mark.parametrize("during_setup", [False, True])
def test_cleanup_cli_failure_does_not_log_sensitive_exception(script, monkeypatch, capsys, during_setup):
    engine = Mock()
    error = RuntimeError("sensitive-connection-details")
    monkeypatch.setattr(script, "setup_engine", Mock(side_effect=error) if during_setup else Mock(return_value=engine))
    monkeypatch.setattr(script, "cleanup_hub_auth", Mock(side_effect=error))
    monkeypatch.setattr("sys.argv", ["cleanup", "--commit"])
    with pytest.raises(SystemExit) as result:
        script.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert "sensitive-connection-details" not in output.out + output.err
    assert json.loads(output.err) == {"status": "failed", "error_type": "RuntimeError"}
    assert engine.dispose.call_count == (0 if during_setup else 1)
