from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from botocore.stub import Stubber

import app.storage.s3 as s3
from app.storage import Part, StorageFailure, UploadMissing

BUCKET, KEY, UPLOAD_ID = "crucible-files", "ds/_files/abc", "upload-1"
SETTINGS = SimpleNamespace(s3_endpoint_url="http://seaweedfs:8333", s3_public_url="https://crucible.lab.example", s3_region="us-east-1")


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(s3, "get_secret", lambda name: {"S3_ACCESS_KEY_ID": "key", "S3_SECRET_ACCESS_KEY": "secret"}[name])
    store = s3.S3Store(SETTINGS)
    with Stubber(store.client) as stub:
        store.stub = stub
        yield store
        stub.assert_no_pending_responses()


def query(url):
    return {name: values[0] for name, values in parse_qs(urlparse(url).query).items()}


def test_clients_use_path_style_signing_and_send_checksums_only_when_required(store):
    for client in (store.client, store.presigner):
        assert client.meta.config.s3 == {"addressing_style": "path"}
        assert client.meta.config.signature_version == "s3v4"
        assert client.meta.config.request_checksum_calculation == "when_required"
        assert client.meta.config.response_checksum_validation == "when_required"
    assert store.client.meta.endpoint_url == "http://seaweedfs:8333"
    assert store.presigner.meta.endpoint_url == "https://crucible.lab.example"


def test_public_address_defaults_to_the_endpoint(monkeypatch):
    monkeypatch.setattr(s3, "get_secret", lambda name: "x")
    store = s3.S3Store(SimpleNamespace(s3_endpoint_url="http://localhost:8333", s3_public_url=None, s3_region="us-east-1"))
    assert store.presigner.meta.endpoint_url == "http://localhost:8333"


def test_start_upload_asks_for_crc32c_part_checksums(store):
    store.stub.add_response("create_multipart_upload", {"UploadId": UPLOAD_ID}, {"Bucket": BUCKET, "Key": KEY, "ContentType": "application/octet-stream", "ChecksumAlgorithm": "CRC32C"})
    assert store.start_upload(BUCKET, KEY) == UPLOAD_ID


def test_part_url_signs_the_checksum_and_length_the_client_must_send(store):
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    url, headers = store.sign_part(BUCKET, KEY, UPLOAD_ID, 7, "AAAAAA==", 64, expires)
    assert url.startswith(f"https://crucible.lab.example/{BUCKET}/{KEY}?")
    params = query(url)
    assert params["X-Amz-SignedHeaders"] == "content-length;host;x-amz-checksum-crc32c"
    assert (params["uploadId"], params["partNumber"]) == (UPLOAD_ID, "7")
    assert 890 <= int(params["X-Amz-Expires"]) <= 900
    assert headers == {"x-amz-checksum-crc32c": "AAAAAA==", "Content-Length": "64"}


def test_finish_sends_each_part_checksum(store):
    parts = [Part(part_number=1, size=10, etag='"a"'), Part(part_number=2, size=3, etag='"b"')]
    expected = {"Bucket": BUCKET, "Key": KEY, "UploadId": UPLOAD_ID, "MultipartUpload": {"Parts": [
        {"PartNumber": 1, "ETag": '"a"', "ChecksumCRC32C": "AAAAAQ=="},
        {"PartNumber": 2, "ETag": '"b"', "ChecksumCRC32C": "AAAAAg=="},
    ]}}
    store.stub.add_response("complete_multipart_upload", {}, expected)
    store.finish_upload(BUCKET, KEY, UPLOAD_ID, parts, {1: "AAAAAQ==", 2: "AAAAAg=="})


def test_list_parts_pages_and_refuses_a_listing_that_goes_backwards(store):
    store.stub.add_response("list_parts", {"Parts": [{"PartNumber": 1001, "Size": 5, "ETag": '"e"'}], "IsTruncated": False}, {"Bucket": BUCKET, "Key": KEY, "UploadId": UPLOAD_ID, "MaxParts": 1000, "PartNumberMarker": 1000})
    assert store.list_parts(BUCKET, KEY, UPLOAD_ID, 1000) == ([Part(part_number=1001, size=5, etag='"e"')], None)
    store.stub.add_response("list_parts", {"Parts": [{"PartNumber": 1, "Size": 5, "ETag": '"e"', "ChecksumCRC32C": "AAAAAQ=="}] * 2, "IsTruncated": False}, {"Bucket": BUCKET, "Key": KEY, "UploadId": UPLOAD_ID, "MaxParts": 1000, "PartNumberMarker": 0})
    assert store.list_parts(BUCKET, KEY, UPLOAD_ID) == ([Part(part_number=1, size=5, etag='"e"', crc32c="AAAAAQ==")], None)
    store.stub.add_response("list_parts", {"Parts": [{"PartNumber": 3, "Size": 5, "ETag": '"e"'}], "IsTruncated": True, "NextPartNumberMarker": 3}, {"Bucket": BUCKET, "Key": KEY, "UploadId": UPLOAD_ID, "MaxParts": 1000, "PartNumberMarker": 5})
    with pytest.raises(StorageFailure, match="invalid_part_listing"):
        store.list_parts(BUCKET, KEY, UPLOAD_ID, 5)


def test_missing_upload_is_reported_and_abort_ignores_it(store):
    store.stub.add_client_error("list_parts", "NoSuchUpload", http_status_code=404)
    with pytest.raises(UploadMissing):
        store.list_parts(BUCKET, KEY, UPLOAD_ID)
    store.stub.add_client_error("abort_multipart_upload", "NoSuchUpload", http_status_code=404)
    store.abort_upload(BUCKET, KEY, UPLOAD_ID)


def test_open_uploads_keeps_only_the_exact_key(store):
    store.stub.add_response("list_multipart_uploads", {"Uploads": [{"Key": KEY, "UploadId": "u1"}, {"Key": KEY + "-other", "UploadId": "u2"}], "IsTruncated": False}, {"Bucket": BUCKET, "Prefix": KEY})
    assert store.open_uploads(BUCKET, KEY) == ["u1"]


@pytest.mark.parametrize("response, version", [({"ContentLength": 13, "ETag": '"abc-2"'}, "abc-2"), ({"ContentLength": 13, "ETag": '"abc"', "VersionId": "v9"}, "v9"), ({"ContentLength": 13, "ETag": '"abc"', "VersionId": "null"}, "abc")])
def test_head_reports_size_no_checksum_and_a_version(store, response, version):
    store.stub.add_response("head_object", response, {"Bucket": BUCKET, "Key": KEY})
    info = store.head(BUCKET, KEY)
    assert (info.size, info.crc32c, info.version) == (13, None, version)


def test_head_of_a_missing_object_is_none_and_other_errors_fail(store):
    store.stub.add_client_error("head_object", "404", http_status_code=404)
    assert store.head(BUCKET, KEY) is None
    store.stub.add_client_error("head_object", "AccessDenied", "secret detail http://seaweedfs:8333", http_status_code=403)
    with pytest.raises(StorageFailure) as raised:
        store.head(BUCKET, KEY)
    assert str(raised.value) == "provider_request_failed"


def test_download_url_carries_the_response_headers(store):
    store.stub.add_response("head_object", {"ContentLength": 3, "ContentType": "application/octet-stream"}, {"Bucket": BUCKET, "Key": KEY})
    url = store.signed_download(BUCKET, KEY, "abc", "results/scan.pdf", "inline", timedelta(hours=1))
    params = query(url)
    assert url.startswith(f"https://crucible.lab.example/{BUCKET}/{KEY}?")
    assert params["response-content-disposition"] == "inline; filename=\"scan.pdf\"; filename*=UTF-8''scan.pdf"
    assert params["response-content-type"] == "application/pdf"
    assert params["X-Amz-Expires"] == "3600"
    assert params["X-Amz-SignedHeaders"] == "host"


def test_download_of_a_missing_object_raises_file_not_found(store):
    store.stub.add_client_error("head_object", "404", http_status_code=404)
    with pytest.raises(FileNotFoundError):
        store.signed_download(BUCKET, KEY, None, "a.txt", "attachment", timedelta(hours=1))


def test_record_files_round_trip(store):
    store.stub.add_response("put_object", {}, {"Bucket": "records", "Key": "ds1/record.json", "Body": b"{}", "ContentType": "application/json"})
    store.put_bytes("records", "ds1/record.json", b"{}", "application/json")
    store.stub.add_client_error("get_object", "NoSuchKey", http_status_code=404)
    assert store.get_text("records", "ds1/record.json") is None


def test_delete_ignores_the_version_and_retire_is_never_done(store):
    store.stub.add_response("delete_object", {}, {"Bucket": BUCKET, "Key": KEY})
    store.delete(BUCKET, KEY, "abc")
    assert store.retires_by_lifecycle is False
    assert store.retire(BUCKET, KEY, "abc", datetime.now(timezone.utc)) is False


def test_store_without_an_endpoint_is_not_configured():
    with pytest.raises(StorageFailure, match="storage_not_configured"):
        s3.S3Store(SimpleNamespace(s3_endpoint_url=None, s3_public_url=None, s3_region="us-east-1"))


def test_get_store_chooses_the_backend(monkeypatch):
    from app import storage

    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://seaweedfs:8333")
    monkeypatch.setattr(s3, "get_secret", lambda name: "x")
    storage.get_store.cache_clear()
    try:
        assert isinstance(storage.get_store(), s3.S3Store)
    finally:
        storage.get_store.cache_clear()
