"""Store configurations for tests: STORAGE_STORES is the only storage setting."""
import json

from app.config import get_settings
from app.storage import get_store


def gcs(id="mf-storage-prod", bucket=None, **extra):
    return {"id": id, "display_name": id, "type": "gcs", "bucket": bucket or id, **extra}


def s3(id, bucket, endpoint_url="http://s3.test:9000", **extra):
    return {"id": id, "display_name": id, "type": "s3", "bucket": bucket, "endpoint_url": endpoint_url, **extra}


def use_stores(monkeypatch, *stores, default=None, secrets=True):
    """Configure these stores (and the S3 secrets they need) for the rest of the test."""
    monkeypatch.setenv("STORAGE_STORES", json.dumps(list(stores)))
    if default is None:
        monkeypatch.delenv("STORAGE_DEFAULT_STORE", raising=False)
    else:
        monkeypatch.setenv("STORAGE_DEFAULT_STORE", default)
    for store in stores:
        if secrets and store["type"] == "s3":
            prefix = "STORAGE_" + store["id"].upper().replace("-", "_")
            monkeypatch.setenv(f"{prefix}_ACCESS_KEY_ID", "test-access-key")
            monkeypatch.setenv(f"{prefix}_SECRET_ACCESS_KEY", "test-secret-key")
    get_settings.cache_clear()
    get_store.cache_clear()


def no_stores(monkeypatch):
    monkeypatch.delenv("STORAGE_STORES", raising=False)
    monkeypatch.delenv("STORAGE_DEFAULT_STORE", raising=False)
    get_settings.cache_clear()
    get_store.cache_clear()
