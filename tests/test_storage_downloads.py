from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import NotFound

from app.storage import StorageFailure
from app.services.storage_downloads import (
    StorageLocationError,
    StorageObject,
    download_headers,
    resolve_storage_object,
    retire_object,
)
import app.storage.gcs as google

DS = "0t3qajrnzhtn7000x6s0x427hg"
MULTIPART = "mf-crucible-multipart-staging"


@pytest.fixture(autouse=True)
def multipart_bucket(monkeypatch):
    monkeypatch.setenv("MULTIPART_UPLOAD_BUCKET", MULTIPART)


@pytest.mark.parametrize("path, generation, expected", [
    (f"mf-storage-prod/{DS}/scan.tif", None, StorageObject("mf-storage-prod", f"{DS}/scan.tif", None)),
    (f"mf-storage-prod/{DS}/sub/dir/scan.tif", None, StorageObject("mf-storage-prod", f"{DS}/sub/dir/scan.tif", None)),
    (f"mf-storage-prod/{DS}//scan.tif", None, StorageObject("mf-storage-prod", f"{DS}//scan.tif", None)),
    (f"mf-storage-prod/{DS}/scan..v2.tif", None, StorageObject("mf-storage-prod", f"{DS}/scan..v2.tif", None)),
    (f"{MULTIPART}/{DS}/_files/abc", "7", StorageObject(MULTIPART, f"{DS}/_files/abc", "7")),
])
def test_paths_under_the_own_dataset_resolve(path, generation, expected):
    assert resolve_storage_object(path, generation, DS) == expected


@pytest.mark.parametrize("path, generation, reason", [
    (None, None, "unparseable"),
    ("", None, "unparseable"),
    ("mf-storage-prod", None, "unparseable"),
    (f"keys-and-certs/{DS}/scan.tif", None, "bucket_not_allowed"),
    ("mf-storage-prod/0t3qajvcgnvx7000h49ya23r7c/scan.tif", None, "outside_dataset"),
    (f"mf-storage-prod/{DS}x/scan.tif", None, "outside_dataset"),
    (f"mf-storage-prod/{DS}/", None, "outside_dataset"),
    (f"mf-storage-prod/{DS}/../other/scan.tif", None, "invalid_segments"),
    (f"mf-storage-prod/{DS}/./scan.tif", None, "invalid_segments"),
    (f"mf-storage-prod//{DS}/scan.tif", None, "invalid_segments"),
    (f"{MULTIPART}/uploads/{DS}/abc/content", "7", "outside_dataset"),
    (f"{MULTIPART}/{DS}/_files/abc", None, "unpublished"),
])
def test_paths_outside_the_rule_are_rejected(path, generation, reason):
    with pytest.raises(StorageLocationError) as error:
        resolve_storage_object(path, generation, DS)
    assert error.value.reason == reason


def test_multipart_bucket_unset_allows_only_the_legacy_bucket(monkeypatch):
    monkeypatch.delenv("MULTIPART_UPLOAD_BUCKET")
    with pytest.raises(StorageLocationError) as error:
        resolve_storage_object(f"{MULTIPART}/{DS}/_files/abc", "7", DS)
    assert error.value.reason == "bucket_not_allowed"
    assert resolve_storage_object(f"mf-storage-prod/{DS}/a.txt", None, DS).bucket == "mf-storage-prod"


@pytest.mark.parametrize("filename, stored, expected_disposition, content_type", [
    ("results/scan 1.pdf", "application/octet-stream", "inline; filename=\"scan 1.pdf\"; filename*=UTF-8''scan%201.pdf", "application/pdf"),
    ("Ångström map.png", None, "inline; filename=\"_ngstr_m map.png\"; filename*=UTF-8''%C3%85ngstr%C3%B6m%20map.png", "image/png"),
    ("report.html", None, "attachment; filename=\"report.html\"; filename*=UTF-8''report.html", "text/html"),
    ("figure.svg", None, "attachment; filename=\"figure.svg\"; filename*=UTF-8''figure.svg", "image/svg+xml"),
    ("data.crucible-unknown", "text/html; charset=utf-8", "attachment; filename=\"data.crucible-unknown\"; filename*=UTF-8''data.crucible-unknown", None),
    ("data.crucible-unknown", "application/octet-stream", "inline; filename=\"data.crucible-unknown\"; filename*=UTF-8''data.crucible-unknown", None),
    ('a"b\\c\r\n.txt', None, "inline; filename=\"a_b_c__.txt\"; filename*=UTF-8''a%22b%5Cc%0D%0A.txt", "text/plain"),
    ("", None, "inline; filename=\"download\"; filename*=UTF-8''download", None),
    (None, None, "inline; filename=\"download\"; filename*=UTF-8''download", None),
])
def test_download_headers(filename, stored, expected_disposition, content_type):
    assert download_headers(filename, stored, disposition="inline") == (expected_disposition, content_type)


@pytest.mark.parametrize("filename, stored, expected_disposition, content_type", [
    ("results/scan 1.pdf", "application/octet-stream", "attachment; filename=\"scan 1.pdf\"; filename*=UTF-8''scan%201.pdf", None),
    ("report.html", None, "attachment; filename=\"report.html\"; filename*=UTF-8''report.html", None),
    ("", None, "attachment; filename=\"download\"; filename*=UTF-8''download", None),
])
def test_download_headers_defaults_to_attachment(filename, stored, expected_disposition, content_type):
    assert download_headers(filename, stored) == (expected_disposition, content_type)
    assert download_headers(filename, stored, disposition="attachment") == (expected_disposition, content_type)


def test_stylesheet_downloads_as_attachment_even_when_inline_requested():
    disposition, _ = download_headers("stylesheet.xsl", None, disposition="inline")
    assert disposition.startswith("attachment;")


def test_rdf_xml_downloads_as_attachment_even_when_inline_requested():
    disposition, content_type = download_headers("graph.rdf", None, disposition="inline")
    assert disposition.startswith("attachment;")
    assert content_type == "application/rdf+xml"


def test_stored_atom_xml_with_unknown_extension_downloads_as_attachment_even_when_inline_requested():
    disposition, content_type = download_headers("feed.crucible-unknown", "application/atom+xml", disposition="inline")
    assert disposition.startswith("attachment;")
    assert content_type is None


UNCONFIGURED = SimpleNamespace(bucket=None, signer_service_account=None, project=None)
HOUR = timedelta(hours=1)


@pytest.fixture
def keyed(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(google, "server_client", lambda: client)
    return client


@pytest.fixture
def store(keyed):
    return google.GoogleStore(UNCONFIGURED)


def _client(keyed, content_type="application/octet-stream"):
    blob = keyed.bucket.return_value.blob.return_value
    blob.content_type = content_type
    blob.generate_signed_url.return_value = "https://signed.example/x"
    return keyed, blob


def test_signed_url_is_generation_pinned_and_carries_headers(store, keyed):
    client, blob = _client(keyed)
    url = store.signed_download("bucket", f"{DS}/_files/abc", "7", "results/scan.pdf", "inline", HOUR)
    assert url == "https://signed.example/x"
    client.bucket.assert_called_once_with("bucket")
    client.bucket.return_value.blob.assert_called_once_with(f"{DS}/_files/abc", generation=7)
    blob.reload.assert_called_once_with(timeout=60)
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert kwargs["version"] == "v4"
    assert kwargs["method"] == "GET"
    assert kwargs["expiration"] == timedelta(hours=1)
    assert kwargs["generation"] == 7
    assert kwargs["response_disposition"] == "inline; filename=\"scan.pdf\"; filename*=UTF-8''scan.pdf"
    assert kwargs["response_type"] == "application/pdf"


def test_unknown_type_keeps_the_stored_type(store, keyed):
    client, blob = _client(keyed)
    store.signed_download("bucket", f"{DS}/data.crucible-unknown", None, "data.crucible-unknown", "inline", HOUR)
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert "response_type" not in kwargs
    assert kwargs["generation"] is None


def test_signed_url_defaults_to_attachment_without_type_override(store, keyed):
    client, blob = _client(keyed, content_type="application/pdf")
    store.signed_download("bucket", f"{DS}/scan.pdf", None, "scan.pdf", "attachment", HOUR)
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"].startswith("attachment;")
    assert "response_type" not in kwargs


def test_signed_url_inline_guesses_type_for_pdf(store, keyed):
    client, blob = _client(keyed, content_type="application/octet-stream")
    store.signed_download("bucket", f"{DS}/scan.pdf", None, "scan.pdf", "inline", HOUR)
    kwargs = blob.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"].startswith("inline;")
    assert kwargs["response_type"] == "application/pdf"


def test_missing_object_raises_file_not_found(store, keyed):
    client, blob = _client(keyed)
    blob.reload.side_effect = NotFound("gone")
    with pytest.raises(FileNotFoundError):
        store.signed_download("bucket", f"{DS}/a.txt", None, "a.txt", "attachment", HOUR)
    blob.generate_signed_url.assert_not_called()


WHEN = datetime(2026, 10, 13, tzinfo=timezone.utc)


def assert_bounded(call, **expected):
    kwargs = dict(call.kwargs)
    retry = kwargs.pop("retry")
    assert kwargs == {**expected, "timeout": 30}
    assert retry._timeout == 30


def test_retire_object_sets_custom_time_pinned_to_generation(store, keyed):
    client = keyed
    blob = client.bucket.return_value.blob.return_value
    assert retire_object(store, f"bucket/{DS}/a.txt", "7", WHEN) is True
    client.bucket.assert_called_once_with("bucket")
    client.bucket.return_value.blob.assert_called_once_with(f"{DS}/a.txt")
    assert blob.custom_time == WHEN
    blob.patch.assert_called_once()
    assert_bounded(blob.patch.call_args, if_generation_match=7)
    blob.reload.assert_not_called()


def test_retire_object_reads_the_generation_of_a_legacy_object(store, keyed):
    blob = keyed.bucket.return_value.blob.return_value
    blob.generation = 42
    assert retire_object(store, f"bucket/{DS}/a.txt", None, WHEN) is True
    blob.reload.assert_called_once()
    assert_bounded(blob.reload.call_args)
    blob.patch.assert_called_once()
    assert_bounded(blob.patch.call_args, if_generation_match=42)


def test_retire_object_missing_object_is_false(store, keyed):
    blob = keyed.bucket.return_value.blob.return_value
    blob.reload.side_effect = NotFound("gone")
    assert retire_object(store, f"bucket/{DS}/a.txt", None, WHEN) is False
    blob.patch.assert_not_called()


def test_retire_object_skips_a_store_without_lifecycle_retirement():
    store = MagicMock(retires_by_lifecycle=False)
    assert retire_object(store, f"bucket/{DS}/a.txt", "7", WHEN) is False
    store.retire.assert_not_called()


def test_record_files_use_the_server_client(store, keyed):
    from google.api_core.exceptions import NotFound
    blob = keyed.bucket.return_value.blob.return_value
    blob.download_as_text.side_effect = NotFound("missing")
    assert store.get_text("records", "ds1/record.json") is None
    blob.download_as_text.side_effect = None
    blob.download_as_text.return_value = "{}"
    assert store.get_text("records", "ds1/record.json") == "{}"
    store.put_bytes("records", "ds1/record.json", b"{}", "application/json")
    blob.upload_from_string.assert_called_once_with(b"{}", content_type="application/json", timeout=60)
    keyed.bucket.assert_called_with("records")
    keyed.bucket.return_value.blob.assert_called_with("ds1/record.json")


def test_signing_errors_stay_server_side(monkeypatch):
    from fastapi import HTTPException
    from app.api.routes import gcs_storage

    def fail(*args, **kwargs):
        raise RuntimeError("https://iamcredentials.googleapis.com signer@mf-crucible.iam.gserviceaccount.com")
    monkeypatch.setattr(gcs_storage, "get_store", lambda: SimpleNamespace(signed_download=fail))
    with pytest.raises(HTTPException) as raised:
        gcs_storage._signed_download("file", DS, f"mf-storage-prod/{DS}/scan.tif", None, "scan.tif")
    assert raised.value.status_code == 503
    assert "googleapis" not in raised.value.detail and "iam" not in raised.value.detail


def test_record_file_errors_become_storage_unavailable(store, keyed):
    from google.api_core.exceptions import ServiceUnavailable
    blob = keyed.bucket.return_value.blob.return_value
    blob.download_as_text.side_effect = ServiceUnavailable("down")
    blob.upload_from_string.side_effect = ServiceUnavailable("down")
    with pytest.raises(StorageFailure, match="provider_unavailable"):
        store.get_text("records", "ds1/record.json")
    with pytest.raises(StorageFailure, match="provider_unavailable"):
        store.put_bytes("bucket", "ds1/_files/empty", b"", "application/octet-stream")
