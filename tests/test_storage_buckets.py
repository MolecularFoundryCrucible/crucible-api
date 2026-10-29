from pathlib import Path

import pytest
import yaml

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


def test_multipart_signer_is_required_only_on_google_storage():
    plain = {"MULTIPART_UPLOAD_ENABLED": "true", "MULTIPART_UPLOAD_BUCKET": "crucible-files", "UPLOAD_BUCKET": "crucible-files"}
    with pytest.raises(ValueError, match="signer and a project"):
        MultipartStorageConfig.from_env(plain)
    assert MultipartStorageConfig.from_env(plain | {"STORAGE_BACKEND": "s3"}).enabled
    with pytest.raises(ValueError, match="explicit bucket"):
        MultipartStorageConfig.from_env({"MULTIPART_UPLOAD_ENABLED": "true", "STORAGE_BACKEND": "s3"})


@pytest.mark.parametrize("environment, bucket", [("main", "mf-storage-prod"), ("staging", "mf-storage-staging")])
def test_deployments_wire_one_bucket_per_environment(environment, bucket):
    config = yaml.safe_load((ROOT / f"k8s-conf/configmap-{environment}.yaml").read_text())["data"]
    deployment = yaml.safe_load((ROOT / f"k8s-conf/deployment-k8s-conf-{environment}.yaml").read_text())
    values = {entry["name"]: entry for entry in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert config["UPLOAD_BUCKET"] == bucket
    for key in ("UPLOAD_BUCKET", "STORAGE_READ_BUCKETS", "MULTIPART_UPLOAD_ENABLED", "MULTIPART_UPLOAD_BUCKET", "MULTIPART_UPLOAD_SIGNER", "MULTIPART_UPLOAD_PROJECT"):
        if key in config:
            assert values[key]["valueFrom"]["configMapKeyRef"] == {"name": f"crucible-api-config-{environment}", "key": key}
    assert config["MULTIPART_UPLOAD_ENABLED"] == "true"
    assert config["MULTIPART_UPLOAD_BUCKET"] == bucket
    assert config["MULTIPART_UPLOAD_SIGNER"] == f"crucible-mpu-signer-{'prod' if environment == 'main' else 'staging'}@mf-crucible.iam.gserviceaccount.com"
    assert config["MULTIPART_UPLOAD_PROJECT"] == "mf-crucible"
    if environment == "staging":
        assert config["STORAGE_READ_BUCKETS"] == "mf-storage-prod"
    else:
        assert "STORAGE_READ_BUCKETS" not in config


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
