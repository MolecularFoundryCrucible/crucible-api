from pathlib import Path

import pytest

from app.services.storage_downloads import StorageLocationError, StorageNotConfigured, allowed_buckets, resolve_storage_object, upload_bucket
from app.utils.multipart_config import MultipartStorageConfig

ROOT = Path(__file__).resolve().parents[1]
DS = "0t3qajrnzhtn7000x6s0x427hg"
MULTIPART = dict(MULTIPART_UPLOAD_ENABLED="true", MULTIPART_UPLOAD_SIGNER="signer@example.iam.gserviceaccount.com", MULTIPART_UPLOAD_PROJECT="project")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("UPLOAD_BUCKET", "STORAGE_READ_BUCKETS", "MULTIPART_UPLOAD_BUCKET"):
        monkeypatch.delenv(name, raising=False)


def test_upload_bucket_has_no_default(monkeypatch):
    with pytest.raises(StorageNotConfigured):
        upload_bucket()
    with pytest.raises(StorageNotConfigured):
        resolve_storage_object(f"mf-storage-prod/{DS}/a.txt", None, DS)
    assert allowed_buckets() == set()


def test_upload_bucket_is_trimmed(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET", " mf-storage-staging ")
    assert upload_bucket() == "mf-storage-staging"


def test_allowed_buckets_combine_upload_multipart_and_extra_read_buckets(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET", "mf-storage-staging")
    monkeypatch.setenv("MULTIPART_UPLOAD_BUCKET", "mf-storage-staging")
    monkeypatch.setenv("STORAGE_READ_BUCKETS", " mf-storage-prod , ,old-bucket")
    assert allowed_buckets() == {"mf-storage-staging", "mf-storage-prod", "old-bucket"}


def test_staging_reads_old_production_records_and_new_staging_objects(monkeypatch):
    monkeypatch.setenv("UPLOAD_BUCKET", "mf-storage-staging")
    monkeypatch.setenv("STORAGE_READ_BUCKETS", "mf-storage-prod")
    assert resolve_storage_object(f"mf-storage-prod/{DS}/a.txt", None, DS).bucket == "mf-storage-prod"
    assert resolve_storage_object(f"mf-storage-staging/{DS}/_files/abc", "3", DS).bucket == "mf-storage-staging"
    with pytest.raises(StorageLocationError):
        resolve_storage_object(f"mf-crucible-multipart-staging/{DS}/_files/abc", "3", DS)


def test_multipart_bucket_must_be_the_upload_bucket():
    with pytest.raises(ValueError, match="UPLOAD_BUCKET"):
        MultipartStorageConfig.from_env(MULTIPART | {"MULTIPART_UPLOAD_BUCKET": "mf-storage-prod"})
    assert MultipartStorageConfig.from_env(MULTIPART | {"MULTIPART_UPLOAD_BUCKET": "mf-storage-prod", "UPLOAD_BUCKET": "mf-storage-prod"}).bucket == "mf-storage-prod"
    assert MultipartStorageConfig.from_env(MULTIPART | {"MULTIPART_UPLOAD_BUCKET": "mf-storage-staging", "UPLOAD_BUCKET": "mf-storage-staging"}).enabled
    with pytest.raises(ValueError, match="UPLOAD_BUCKET"):
        MultipartStorageConfig.from_env(MULTIPART | {"MULTIPART_UPLOAD_BUCKET": "mf-crucible-multipart-staging", "UPLOAD_BUCKET": "mf-storage-staging"})


def test_multipart_needs_no_signer():
    plain = {"MULTIPART_UPLOAD_ENABLED": "true", "MULTIPART_UPLOAD_BUCKET": "crucible-files", "UPLOAD_BUCKET": "crucible-files"}
    assert MultipartStorageConfig.from_env(plain).signer_service_account is None
    assert MultipartStorageConfig.from_env(plain | {"STORAGE_BACKEND": "s3"}).enabled
    with pytest.raises(ValueError, match="explicit bucket"):
        MultipartStorageConfig.from_env({"MULTIPART_UPLOAD_ENABLED": "true", "STORAGE_BACKEND": "s3"})


def test_multipart_config_reads_the_upload_bucket_from_settings(monkeypatch, tmp_path):
    from app.config import get_settings

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "UPLOAD_BUCKET").write_text("bucket-from-file")
    monkeypatch.setenv("CRUCIBLE_SECRETS_DIR", str(secrets))
    for name, value in (MULTIPART | {"MULTIPART_UPLOAD_BUCKET": "bucket-from-file"}).items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    assert MultipartStorageConfig.from_env().bucket == "bucket-from-file"
