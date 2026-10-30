from types import SimpleNamespace
from unittest.mock import MagicMock
from xml.etree import ElementTree as ET

import pytest
from google.api_core.exceptions import ServiceUnavailable
from google.auth.credentials import Signing
from google.auth.exceptions import DefaultCredentialsError, RefreshError
from google.oauth2 import service_account

from app.storage import Part, StorageFailure, UploadMissing
from app.config import get_settings
from app.storage import gcs
from app.storage.gcs import GoogleStore
from app.services.multipart import now


def adapter():
    storage = GoogleStore.__new__(GoogleStore)
    storage.config = SimpleNamespace(bucket="dedicated")
    storage.credentials = MagicMock(spec=Signing)
    storage.client = MagicMock()
    storage.http = MagicMock()
    return storage


BUCKET, KEY, UPLOAD_ID = "dedicated", "uploads/ds/id/content", "provider-id"


def refuse_default(*args, **kwargs):
    raise AssertionError("google.auth.default called")


def test_multipart_is_refused_without_its_bucket(monkeypatch):
    monkeypatch.setattr("app.storage.gcs.google.auth.default", refuse_default)
    storage = GoogleStore(SimpleNamespace(bucket=None, signer_service_account=None, project=None))
    with pytest.raises(StorageFailure, match="storage_not_configured"):
        storage.start_upload(BUCKET, KEY)
    with pytest.raises(StorageFailure):
        storage.sign_part(BUCKET, KEY, UPLOAD_ID, 1, "AAAAAA==", 1, now())


def test_a_key_file_signs_and_calls_google_with_a_scoped_token():
    storage = GoogleStore(SimpleNamespace(bucket="dedicated", signer_service_account=None, project=None))
    url, headers = storage.sign_part(BUCKET, KEY, UPLOAD_ID, 1, "AAAAAA==", 1, now())
    assert storage.credentials is gcs.server_credentials()
    assert isinstance(storage.credentials, service_account.Credentials)
    assert list(storage.credentials.scopes) == [gcs.CLOUD_PLATFORM]
    assert "X-Goog-Credential=crucible-test%40test-project.iam.gserviceaccount.com" in url
    assert (headers["x-goog-hash"], headers["Content-Length"]) == ("crc32c=AAAAAA==", "1")


class TokenOnly:
    """Credentials without a private key, as on Compute Engine, Cloud Run, or GKE with a Google service account."""

    def __init__(self, email):
        self.service_account_email, self.token, self.valid = email, None, False
        self.universe_domain = "googleapis.com"

    def refresh(self, request):
        self.token, self.valid = "access-token", True


def test_signing_uses_the_key_or_impersonation_locally():
    signer = MagicMock(spec=Signing)
    assert gcs.signing(signer) == {"credentials": signer}


def test_signing_without_a_key_goes_through_sign_blob():
    assert gcs.signing(TokenOnly("server@lab.iam.gserviceaccount.com")) == {"service_account_email": "server@lab.iam.gserviceaccount.com", "access_token": "access-token"}


@pytest.mark.parametrize("email", [None, "default"])
def test_signing_needs_a_service_account(email):
    with pytest.raises(StorageFailure, match="signer_required"):
        gcs.signing(TokenOnly(email))


def test_an_attached_account_is_the_identity_and_signs_through_sign_blob(monkeypatch):
    identity = TokenOnly("server@lab.iam.gserviceaccount.com")
    monkeypatch.setattr("app.storage.gcs.google.auth.default", lambda scopes: (identity, "lab"))
    assert gcs.server_credentials() is identity
    assert gcs.signing(gcs.server_credentials())["service_account_email"] == "server@lab.iam.gserviceaccount.com"


def test_with_a_signer_every_operation_acts_as_it(monkeypatch):
    monkeypatch.setenv("MULTIPART_UPLOAD_SIGNER", "signer@lab.iam.gserviceaccount.com")
    monkeypatch.setattr("app.storage.gcs.google.auth.default", lambda scopes: (TokenOnly("workload"), "lab"))
    identity = gcs.server_credentials()
    assert identity.signer_email == "signer@lab.iam.gserviceaccount.com"
    assert identity._lifetime == 3600
    assert identity._target_scopes == [gcs.READ_WRITE]
    assert gcs.server_client()._credentials is identity
    storage = GoogleStore(SimpleNamespace(bucket="dedicated", signer_service_account="signer@lab.iam.gserviceaccount.com", project=None))
    storage.connect()
    assert storage.credentials is identity and storage.client is gcs.server_client()
    assert gcs.signing(identity) == {"credentials": identity}


def test_signing_binds_destination_method_checksum_length_and_expiry():
    storage = adapter()
    expires = now()
    _, headers = storage.sign_part(BUCKET, KEY, UPLOAD_ID, 7, "AAAAAA==", 64, expires)
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
        storage.request("POST", BUCKET, KEY)
    assert storage.http.request.call_args.kwargs["allow_redirects"] is False
    assert storage.http.request.call_args.kwargs["timeout"] == (10, 240)


def test_list_parts_paginates_and_checks_progress():
    storage = adapter()
    storage.request = MagicMock(return_value=ET.fromstring('<ListPartsResult><Part><PartNumber>1001</PartNumber><Size>5</Size><ETag>opaque</ETag></Part><IsTruncated>false</IsTruncated></ListPartsResult>'))
    parts, marker = storage.list_parts(BUCKET, KEY, UPLOAD_ID, 1000)
    assert parts == [Part(part_number=1001, size=5, etag="opaque")]
    assert marker is None
    assert storage.request.call_args.kwargs["params"]["part-number-marker"] == "1000"
    storage.request.return_value = ET.fromstring('<ListPartsResult><IsTruncated>true</IsTruncated><NextPartNumberMarker>1000</NextPartNumberMarker></ListPartsResult>')
    with pytest.raises(StorageFailure):
        storage.list_parts(BUCKET, KEY, UPLOAD_ID, 1000)


def test_upload_listing_keeps_exact_key_and_follows_both_markers():
    storage = adapter()
    storage.request = MagicMock(side_effect=[ET.fromstring('<ListMultipartUploadsResult><Upload><Key>uploads/ds/id/content-extra</Key><UploadId>wrong</UploadId></Upload><IsTruncated>true</IsTruncated><NextKeyMarker>uploads/ds/id/content</NextKeyMarker><NextUploadIdMarker>a</NextUploadIdMarker></ListMultipartUploadsResult>'), ET.fromstring('<ListMultipartUploadsResult><Upload><Key>uploads/ds/id/content</Key><UploadId>correct</UploadId></Upload><IsTruncated>false</IsTruncated></ListMultipartUploadsResult>')])
    assert storage.open_uploads(BUCKET, KEY) == ["correct"]
    assert storage.request.call_args.kwargs["params"]["key-marker"] == KEY
    assert storage.request.call_args.kwargs["params"]["upload-id-marker"] == "a"


def test_delete_is_generation_pinned():
    storage = adapter()
    storage.delete(BUCKET, KEY, "123")
    blob = storage.client.bucket.return_value.blob.return_value
    blob.delete.assert_called_once_with(if_generation_match=123, timeout=60)


def test_another_bucket_is_refused():
    storage = adapter()
    with pytest.raises(StorageFailure):
        storage.head("other", KEY)
    with pytest.raises(StorageFailure):
        storage.sign_part("other", KEY, UPLOAD_ID, 1, "AAAAAA==", 1, now())


def test_head_reports_size_checksum_and_generation_as_version():
    storage = adapter()
    blob = storage.client.bucket.return_value.blob.return_value
    blob.size, blob.crc32c, blob.generation = 5, "AAAAAA==", 1234
    info = storage.head(BUCKET, KEY)
    assert (info.size, info.crc32c, info.version) == (5, "AAAAAA==", "1234")
    blob.reload.assert_called_once_with(timeout=60)


def test_finish_sends_part_numbers_and_etags():
    storage = adapter()
    storage.request = MagicMock()
    storage.finish_upload(BUCKET, KEY, UPLOAD_ID, [Part(1, 5, "a"), Part(2, 3, "b")], {1: "x", 2: "y"})
    root = ET.fromstring(storage.request.call_args.kwargs["data"])
    assert [(p.findtext("PartNumber"), p.findtext("ETag")) for p in root.findall("Part")] == [("1", "a"), ("2", "b")]
    assert storage.request.call_args.kwargs["params"] == {"uploadId": UPLOAD_ID}


@pytest.mark.parametrize("failure", [ServiceUnavailable("busy"), RefreshError("token")])
def test_head_and_delete_report_google_failures_as_storage_failures(failure):
    storage = adapter()
    blob = storage.client.bucket.return_value.blob.return_value
    blob.reload.side_effect = failure
    blob.delete.side_effect = failure
    with pytest.raises(StorageFailure, match="provider_unavailable"):
        storage.head(BUCKET, KEY)
    with pytest.raises(StorageFailure, match="provider_unavailable"):
        storage.delete(BUCKET, KEY, "7")


def test_missing_credentials_make_multipart_unavailable(monkeypatch):
    def default(**kwargs):
        raise DefaultCredentialsError("no credentials")
    monkeypatch.setattr("app.storage.gcs.google.auth.default", default)
    storage = GoogleStore(SimpleNamespace(bucket="dedicated", signer_service_account=None, project=None))
    with pytest.raises(StorageFailure, match="provider_unavailable"):
        storage.start_upload(BUCKET, KEY)


def test_tokens_that_leave_the_server_are_limited_to_storage():
    assert list(gcs.server_credentials().scopes) == [gcs.CLOUD_PLATFORM]
    assert list(gcs.storage_scoped(gcs.server_credentials()).scopes) == [gcs.READ_WRITE]
