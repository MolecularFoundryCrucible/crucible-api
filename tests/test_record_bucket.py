from unittest.mock import MagicMock

import pytest

import app.utils.gcs_record_utils as gcs_record_utils


@pytest.fixture
def storage(monkeypatch):
    client = MagicMock()
    client.bucket.return_value.blob.return_value.exists.return_value = False
    monkeypatch.setattr(gcs_record_utils, "_get_client", lambda: client)
    return client


@pytest.mark.parametrize("write, name", [(gcs_record_utils.write_record, "record.json"), (gcs_record_utils.write_metadata, "metadata.json")])
def test_writes_go_to_the_configured_bucket(storage, monkeypatch, write, name):
    monkeypatch.setenv("RECORD_BUCKET", "mf-record-staging")
    write("ds1", {"a": 1}, "actor", "create")
    storage.bucket.assert_called_once_with("mf-record-staging")
    storage.bucket.return_value.blob.assert_called_once_with(f"ds1/{name}")
    storage.bucket.return_value.blob.return_value.upload_from_string.assert_called_once()


@pytest.mark.parametrize("value", [None, "", "  "])
def test_writes_are_skipped_without_a_bucket(storage, monkeypatch, caplog, value):
    if value is None:
        monkeypatch.delenv("RECORD_BUCKET", raising=False)
    else:
        monkeypatch.setenv("RECORD_BUCKET", value)
    gcs_record_utils.write_record("ds1", {"a": 1}, "actor", "create")
    gcs_record_utils.write_metadata("ds1", {"a": 1}, "actor", "create")
    storage.bucket.assert_not_called()
    assert "RECORD_BUCKET is not set" in caplog.text


def test_each_environment_has_its_own_bucket():
    from pathlib import Path
    import yaml
    root = Path(__file__).resolve().parents[1] / "k8s-conf"
    buckets = {env: yaml.safe_load((root / f"configmap-{env}.yaml").read_text())["data"]["RECORD_BUCKET"] for env in ("main", "staging")}
    assert buckets == {"main": "mf-record-prod", "staging": "mf-record-staging"}
