from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.storage.gcs as google
import app.utils.gcs_record_utils as gcs_record_utils


@pytest.fixture
def storage(monkeypatch):
    client = MagicMock()
    client.bucket.return_value.blob.return_value.exists.return_value = False
    monkeypatch.setattr(google, "server_client", lambda: client)
    store = google.GoogleStore(SimpleNamespace(bucket=None, signer_service_account=None, project=None))
    monkeypatch.setattr(gcs_record_utils, "get_default_store", lambda: store)
    return client


@pytest.mark.parametrize("write, name", [(gcs_record_utils.write_record, "record.json"), (gcs_record_utils.write_metadata, "metadata.json")])
def test_writes_go_to_the_configured_bucket(storage, monkeypatch, write, name):
    monkeypatch.setenv("RECORD_BUCKET", "mf-record-staging")
    write("ds1", {"a": 1}, "actor", "create")
    assert {call.args for call in storage.bucket.call_args_list} == {("mf-record-staging",)}
    assert {call.args for call in storage.bucket.return_value.blob.call_args_list} == {(f"ds1/{name}",)}
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
