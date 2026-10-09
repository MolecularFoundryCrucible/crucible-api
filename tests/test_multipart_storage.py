from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest

from app.services.multipart_storage import MultipartStorage, StorageFailure, UploadMissing
from app.services.multipart import now


def adapter():
    storage = MultipartStorage.__new__(MultipartStorage)
    storage.config = SimpleNamespace(bucket="dedicated")
    storage.credentials = object()
    storage.client = MagicMock()
    storage.http = MagicMock()
    return storage


def upload():
    return SimpleNamespace(bucket="dedicated", gcs_path="uploads/ds/id/content", provider_upload_id="provider-id")


def test_signing_binds_destination_method_checksum_length_and_expiry():
    storage = adapter()
    expires = now()
    _, headers = storage.sign_part(upload(), 7, "AAAAAA==", 64, expires)
    storage.client.bucket.assert_called_once_with("dedicated")
    storage.client.bucket.return_value.blob.assert_called_once_with("uploads/ds/id/content", generation=None)
    args = storage.client.bucket.return_value.blob.return_value.generate_signed_url.call_args.kwargs
    assert args["method"] == "PUT"
    assert args["expiration"] == expires
    assert args["query_parameters"] == {"uploadId": "provider-id", "partNumber": "7"}
    assert headers == {"Content-Length": "64", "x-goog-hash": "crc32c=AAAAAA=="}
    assert args["headers"] == headers


@pytest.mark.parametrize("status,body,exception", [(200, b"<Error><Code>InvalidPart</Code></Error>", StorageFailure), (404, b"<Error><Code>NoSuchUpload</Code></Error>", UploadMissing), (200, b'<!DOCTYPE x [<!ENTITY a "b">]><x/>', StorageFailure), (302, b"", StorageFailure)])
def test_xml_errors_including_http_200_are_rejected(status, body, exception):
    storage = adapter()
    storage.http.request.return_value = SimpleNamespace(status_code=status, content=body)
    with pytest.raises(exception):
        storage.request("POST", upload())
    assert storage.http.request.call_args.kwargs["allow_redirects"] is False
    assert storage.http.request.call_args.kwargs["timeout"] == (10, 240)


def test_list_parts_paginates_and_checks_progress():
    storage = adapter()
    storage.request = MagicMock(return_value=ET.fromstring('<ListPartsResult><Part><PartNumber>1001</PartNumber><Size>5</Size><ETag>opaque</ETag></Part><IsTruncated>false</IsTruncated></ListPartsResult>'))
    parts, marker = storage.list_parts(upload(), 1000)
    assert parts[0]["part_number"] == 1001
    assert marker is None
    assert storage.request.call_args.kwargs["params"]["part-number-marker"] == "1000"
    storage.request.return_value = ET.fromstring('<ListPartsResult><IsTruncated>true</IsTruncated><NextPartNumberMarker>1000</NextPartNumberMarker></ListPartsResult>')
    with pytest.raises(StorageFailure):
        storage.list_parts(upload(), 1000)


def test_upload_listing_keeps_exact_key_and_follows_both_markers():
    storage = adapter()
    storage.request = MagicMock(side_effect=[ET.fromstring('<ListMultipartUploadsResult><Upload><Key>uploads/ds/id/content-extra</Key><UploadId>wrong</UploadId></Upload><IsTruncated>true</IsTruncated><NextKeyMarker>uploads/ds/id/content</NextKeyMarker><NextUploadIdMarker>a</NextUploadIdMarker></ListMultipartUploadsResult>'), ET.fromstring('<ListMultipartUploadsResult><Upload><Key>uploads/ds/id/content</Key><UploadId>correct</UploadId></Upload><IsTruncated>false</IsTruncated></ListMultipartUploadsResult>')])
    assert storage.uploads(upload()) == ["correct"]
    assert storage.request.call_args.kwargs["params"]["key-marker"] == upload().gcs_path
    assert storage.request.call_args.kwargs["params"]["upload-id-marker"] == "a"


def test_delete_and_download_are_generation_pinned():
    storage = adapter()
    storage.delete(upload(), "123")
    blob = storage.client.bucket.return_value.blob.return_value
    blob.delete.assert_called_once_with(if_generation_match=123, timeout=60)
    storage.download("dedicated", upload().gcs_path, "123", now())
    assert blob.generate_signed_url.call_args.kwargs["generation"] == "123"
    with pytest.raises(StorageFailure):
        storage.download("legacy-bucket", "file", "123", now())
