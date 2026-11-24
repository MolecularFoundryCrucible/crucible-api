import json

import pytest
from google.auth.credentials import AnonymousCredentials

from app.config import ConfigurationError, check_settings, get_settings
from app.storage import StoreUnavailable, get_store
from app.storage import gcs, s3

MAIN = {"id": "main", "display_name": "Foundry storage", "type": "gcs", "bucket": "mf-storage"}
VM = {"id": "vm", "display_name": "Lab VM", "type": "s3", "bucket": "crucible", "endpoint_url": "http://vm.test:9000"}


def use_stores(monkeypatch, *stores, default=None):
    monkeypatch.delenv("UPLOAD_BUCKET", raising=False)
    monkeypatch.setenv("STORAGE_STORES", json.dumps(list(stores)))
    if default:
        monkeypatch.setenv("STORAGE_DEFAULT_STORE", default)
    get_settings.cache_clear()


def test_old_settings_define_the_default_store(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("UPLOAD_BUCKET", "files")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://s3.test")
    storage = get_settings().storage
    assert [(s.id, s.type, s.bucket, s.endpoint_url) for s in storage.stores] == [("default", "s3", "files", "http://s3.test")]
    assert storage.default_store_id == "default"
    monkeypatch.delenv("STORAGE_BACKEND")
    monkeypatch.delenv("S3_ENDPOINT_URL")
    get_settings.cache_clear()
    assert [(s.id, s.type, s.bucket, s.project) for s in get_settings().storage.stores] == [("default", "gcs", "files", "crucible-prod")]


def test_a_store_list_replaces_them(monkeypatch):
    use_stores(monkeypatch, MAIN, VM, default="main")
    storage = get_settings().storage
    assert [s.id for s in storage.stores] == ["main", "vm"]
    assert storage.store(None).id == "main"
    assert storage.store("vm").bucket == "crucible"
    assert storage.store("gone") is None


@pytest.mark.parametrize("stores, default, extra", [
    ([MAIN, {**VM, "id": "main"}], "main", {}),
    ([{**MAIN, "id": "Main"}], None, {}),
    ([{**MAIN, "colour": "red"}], None, {}),
    ([{key: value for key, value in VM.items() if key != "endpoint_url"}], None, {}),
    ([MAIN], "vm", {}),
    ([MAIN, VM], None, {}),
    ([MAIN], None, {"UPLOAD_BUCKET": "mf-storage"}),
    ([MAIN], None, {"STORAGE_BACKEND": "gcs"}),
], ids=["duplicate id", "uppercase id", "unknown field", "s3 without endpoint", "unknown default", "no default among two", "with UPLOAD_BUCKET", "with STORAGE_BACKEND"])
def test_invalid_store_lists_are_refused(monkeypatch, stores, default, extra):
    use_stores(monkeypatch, *stores, default=default)
    for name, value in extra.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    with pytest.raises(ConfigurationError):
        check_settings()


def test_each_s3_store_reads_its_own_secrets(monkeypatch):
    use_stores(monkeypatch, MAIN, VM, default="main")
    monkeypatch.setenv("STORAGE_VM_ACCESS_KEY_ID", "vm-access")
    monkeypatch.setenv("STORAGE_VM_SECRET_ACCESS_KEY", "vm-secret")
    sessions = []
    monkeypatch.setattr(s3.boto3.session, "Session", lambda **kwargs: sessions.append(kwargs) or type("S", (), {"client": lambda *a, **k: None})())
    store = get_store("vm")
    assert isinstance(store, s3.S3Store)
    assert sessions == [{"aws_access_key_id": "vm-access", "aws_secret_access_key": "vm-secret", "region_name": "us-east-1"}]


def test_a_missing_store_secret_is_a_missing_setting(monkeypatch):
    use_stores(monkeypatch, MAIN, VM, default="main")
    with pytest.raises(ConfigurationError, match="STORAGE_VM_ACCESS_KEY_ID"):
        check_settings()


def test_each_gcs_store_acts_as_its_own_service_account(monkeypatch):
    use_stores(monkeypatch, {**MAIN, "service_account": "a@p.iam.gserviceaccount.com"}, {**MAIN, "id": "b", "bucket": "other"}, default="main")
    key = {"type": "service_account", "client_email": "b@p.iam.gserviceaccount.com"}
    monkeypatch.setenv("STORAGE_B_CREDENTIALS", json.dumps(key))
    source = AnonymousCredentials()
    monkeypatch.setattr(gcs.google.auth, "default", lambda scopes: (source, "p"))
    monkeypatch.setattr(gcs.service_account.Credentials, "from_service_account_info", lambda info, scopes: ("key", info["client_email"]))
    storage = get_settings().storage
    impersonated = gcs.store_credentials(storage.store("main"))
    assert isinstance(impersonated._source_credentials, AnonymousCredentials)
    assert impersonated._target_principal == "a@p.iam.gserviceaccount.com"
    assert gcs.store_credentials(storage.store("b")) == ("key", "b@p.iam.gserviceaccount.com")


def test_the_compatibility_store_still_acts_as_the_upload_signer(monkeypatch):
    monkeypatch.setenv("MULTIPART_UPLOAD_SIGNER", "signer@p.iam.gserviceaccount.com")
    get_settings.cache_clear()
    source = AnonymousCredentials()
    monkeypatch.setattr(gcs.google.auth, "default", lambda scopes: (source, "p"))
    credentials = gcs.store_credentials(get_settings().storage.store(None))
    assert credentials._target_principal == "signer@p.iam.gserviceaccount.com"


def test_an_unconfigured_store_is_unavailable(monkeypatch):
    use_stores(monkeypatch, MAIN)
    with pytest.raises(StoreUnavailable) as raised:
        get_store("gone")
    assert raised.value.store_id == "gone"
