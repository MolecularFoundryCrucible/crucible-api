import json

import pytest

from app.config import ConfigurationError, check_settings, get_settings
from app.services.storage_downloads import StorageLocationError, resolve_storage_object, store_buckets
from tests.stores import gcs, use_stores

DS = "0t3qajrnzhtn7000x6s0x427hg"


def test_a_store_reads_its_bucket_and_its_read_buckets(monkeypatch):
    use_stores(monkeypatch, gcs("staging", bucket="mf-storage-staging", read_buckets=["mf-storage-prod", "old-bucket"]))
    store = get_settings().storage.store("staging")
    assert store_buckets(store) == {"mf-storage-staging", "mf-storage-prod", "old-bucket"}
    assert resolve_storage_object(store, f"mf-storage-prod/{DS}/a.txt", None, DS).bucket == "mf-storage-prod"
    assert resolve_storage_object(store, f"mf-storage-staging/{DS}/_files/abc", "3", DS).bucket == "mf-storage-staging"
    with pytest.raises(StorageLocationError):
        resolve_storage_object(store, f"mf-crucible-multipart-staging/{DS}/_files/abc", "3", DS)


@pytest.mark.parametrize("name", ["UPLOAD_BUCKET", "STORAGE_BACKEND", "S3_ENDPOINT_URL", "MULTIPART_UPLOAD_SIGNER", "STORAGE_READ_BUCKETS", "S3_SECRET_ACCESS_KEY_VERSION"])
def test_a_retired_single_store_setting_stops_the_server(monkeypatch, name):
    monkeypatch.setenv(name, "x")
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match=f"{name.removesuffix('_VERSION')} \\(use"):
        check_settings()


def test_a_retired_setting_in_the_env_file_or_secrets_directory_stops_the_server(monkeypatch, tmp_path):
    from app import config
    env_file = tmp_path / "local.env"
    env_file.write_text("UPLOAD_BUCKET=old\n")
    monkeypatch.setenv("UPLOAD_BUCKET", "")
    monkeypatch.delenv("UPLOAD_BUCKET")
    monkeypatch.setenv("DB_CONF_FILE", str(env_file))
    monkeypatch.setattr(config, "_loaded_env_files", set())
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match="UPLOAD_BUCKET \\(use"):
        check_settings()
    monkeypatch.delenv("UPLOAD_BUCKET")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "S3_ACCESS_KEY_ID").write_text("old")
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(secrets))
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError, match="S3_ACCESS_KEY_ID \\(use"):
        check_settings()


def test_the_stores_can_be_read_from_a_file(monkeypatch, tmp_path):
    stores = tmp_path / "stores.json"
    stores.write_text(json.dumps([gcs("mf-storage-prod", project="mf-crucible")]))
    monkeypatch.delenv("STORAGE_STORES")
    monkeypatch.setenv("STORAGE_STORES_FILE", str(stores))
    get_settings.cache_clear()
    assert [(store.id, store.project) for store in get_settings().storage.stores] == [("mf-storage-prod", "mf-crucible")]


def test_the_documented_example_stores_are_valid(monkeypatch):
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / "docs" / "stores.example.json"
    monkeypatch.delenv("STORAGE_STORES")
    monkeypatch.setenv("STORAGE_STORES_FILE", str(example))
    monkeypatch.setenv("STORAGE_DEFAULT_STORE", "mf-storage-prod")
    get_settings.cache_clear()
    assert [store.type for store in get_settings().storage.stores] == ["gcs", "s3"]
