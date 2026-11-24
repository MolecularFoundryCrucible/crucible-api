import json
import threading
from functools import lru_cache
from urllib.parse import quote
from xml.etree import ElementTree as ET

import google.auth
from google.auth import impersonated_credentials
from google.oauth2 import service_account
from google.auth.credentials import Scoped, Signing
from google.auth.transport.requests import AuthorizedSession, Request
from google.cloud import storage
from google.api_core.exceptions import NotFound

from app.config import get_settings, optional_secret
from app.services.storage_downloads import download_headers
from app.storage.base import ObjectInfo, Part, StorageFailure, UploadMissing, check_part_listing


def field(root, name):
    return root.findtext("{*}" + name)


CLOUD_PLATFORM = "https://www.googleapis.com/auth/cloud-platform"
READ_WRITE = "https://www.googleapis.com/auth/devstorage.read_write"


@lru_cache
def store_credentials(store):
    """A store's Google identity: its key file (secret STORAGE_{ID}_CREDENTIALS) when given, else
    Application Default Credentials (an attached service account, Workload Identity, or a
    GOOGLE_APPLICATION_CREDENTIALS key file), acting as the store's service_account when it names one,
    which an identity that cannot sign URLs needs. Impersonation asks for the one-hour maximum."""
    key = optional_secret(store.credentials_name) if store.credentials_name else None
    if key:
        source = service_account.Credentials.from_service_account_info(json.loads(key), scopes=[CLOUD_PLATFORM])
    else:
        source, _ = google.auth.default(scopes=[CLOUD_PLATFORM])
    if not store.service_account:
        return source
    return impersonated_credentials.Credentials(source_credentials=source, target_principal=store.service_account, target_scopes=[READ_WRITE], lifetime=3600)


@lru_cache
def store_client(store):
    return storage.Client(project=store.project, credentials=store_credentials(store))


def default_store():
    storage = get_settings().storage
    return storage.store(storage.default_store_id)


@lru_cache
def server_credentials():
    """The default store's identity."""
    return store_credentials(default_store())


def storage_scoped(credentials):
    """The credentials limited to Cloud Storage read and write, for tokens that leave the server."""
    return credentials.with_scopes([READ_WRITE]) if isinstance(credentials, Scoped) else credentials


@lru_cache
def server_client():
    return store_client(default_store())


def signing(credentials):
    """generate_signed_url arguments that sign as these credentials: locally with a key or through
    impersonation, else with IAM signBlob as the service account behind an access token."""
    if isinstance(credentials, Signing):
        return {"credentials": credentials}
    if not credentials.valid:
        credentials.refresh(Request())
    email = getattr(credentials, "service_account_email", None)
    if not email or email == "default":
        raise StorageFailure("signer_required")
    return {"service_account_email": email, "access_token": credentials.token}


class GoogleStore:
    """Google Cloud Storage as the store's identity for every operation. Without a store (tests), the
    default store's identity and the multipart config's bucket."""

    def __init__(self, config, store=None):
        self.config = config
        self.store = store
        self.bucket = store.bucket if store is not None else config.bucket
        self.credentials = self.client = self.http = None
        self.connecting = threading.Lock()

    def identity(self):
        return store_credentials(self.store) if self.store is not None else server_credentials()

    def storage_client(self):
        return store_client(self.store) if self.store is not None else server_client()

    def connect(self):
        """Set up the multipart session on the first multipart call, so the rest never needs it."""
        with self.connecting:
            if self.http is not None:
                return
            if not self.bucket:
                raise StorageFailure("storage_not_configured")
            try:
                self.credentials, self.client = self.identity(), self.storage_client()
            except Exception:
                raise StorageFailure("provider_unavailable") from None
            self.http = AuthorizedSession(self.credentials)

    def blob(self, bucket, key, generation=None):
        self.check_bucket(bucket)
        return self.client.bucket(bucket).blob(key, generation=generation)

    def check_bucket(self, bucket):
        if self.http is None:
            self.connect()
        if bucket != self.bucket:
            raise StorageFailure("bucket_mismatch")

    def request(self, method, bucket, key=None, **kwargs):
        self.check_bucket(bucket)
        url = f"https://storage.googleapis.com/{quote(bucket, safe='')}"
        url += "/" if key is None else "/" + quote(key, safe="/")
        try:
            response = self.http.request(method, url, timeout=(10, 240), allow_redirects=False, **kwargs)
            if len(response.content) > 4 * 1024 * 1024:
                raise StorageFailure("oversized_provider_response")
            content = response.content
            if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                raise StorageFailure("invalid_provider_xml")
            root = ET.fromstring(content) if content.strip() else ET.Element("Empty")
            if response.status_code == 404 and field(root, "Code") == "NoSuchUpload":
                raise UploadMissing("upload_missing")
            if response.status_code not in {200, 204} or root.tag.rsplit("}", 1)[-1] == "Error":
                raise StorageFailure("provider_request_failed")
            return root
        except (StorageFailure, UploadMissing):
            raise
        except Exception:
            raise StorageFailure("provider_unavailable") from None

    def start_upload(self, bucket, key):
        root = self.request("POST", bucket, key, params={"uploads": ""}, data=b"", headers={"Content-Type": "application/octet-stream"})
        identifier = field(root, "UploadId")
        if not identifier:
            raise StorageFailure("missing_provider_upload_id")
        return identifier

    def open_uploads(self, bucket, key):
        params = {"uploads": "", "prefix": key, "max-uploads": "1000"}
        identifiers = []
        seen = set()
        for _ in range(100):
            root = self.request("GET", bucket, params=params)
            identifiers.extend(field(item, "UploadId") for item in root.findall("{*}Upload") if field(item, "Key") == key)
            if field(root, "IsTruncated") != "true":
                return identifiers
            marker = (field(root, "NextKeyMarker"), field(root, "NextUploadIdMarker"))
            if not all(marker) or marker in seen:
                break
            seen.add(marker)
            params.update({"key-marker": marker[0], "upload-id-marker": marker[1]})
        raise StorageFailure("invalid_upload_pagination")

    def list_parts(self, bucket, key, upload_id, marker=0, limit=1000):
        root = self.request("GET", bucket, key, params={"uploadId": upload_id, "max-parts": str(limit), "part-number-marker": str(marker)})
        try:
            parts = [Part(part_number=int(field(part, "PartNumber")), size=int(field(part, "Size")), etag=field(part, "ETag")) for part in root.findall("{*}Part")]
            next_marker = int(field(root, "NextPartNumberMarker")) if field(root, "IsTruncated") == "true" else None
        except (TypeError, ValueError):
            raise StorageFailure("invalid_part_listing") from None
        check_part_listing(parts, next_marker, marker, limit)
        return parts, next_marker

    def sign_part(self, bucket, key, upload_id, number, crc32c, size, expires_at):
        headers = {"x-goog-hash": f"crc32c={crc32c}", "Content-Length": str(size)}
        try:
            url = self.blob(bucket, key).generate_signed_url(version="v4", expiration=expires_at, method="PUT", headers=headers, query_parameters={"uploadId": upload_id, "partNumber": str(number)}, **signing(self.credentials))
            return url, headers
        except StorageFailure:
            raise
        except Exception:
            raise StorageFailure("signing_failed") from None

    def finish_upload(self, bucket, key, upload_id, parts, crc32c_by_part):
        root = ET.Element("CompleteMultipartUpload")
        for item in parts:
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(item.part_number)
            ET.SubElement(part, "ETag").text = item.etag
        self.request("POST", bucket, key, params={"uploadId": upload_id}, data=ET.tostring(root), headers={"Content-Type": "application/xml"})

    def abort_upload(self, bucket, key, upload_id):
        try:
            self.request("DELETE", bucket, key, params={"uploadId": upload_id})
        except UploadMissing:
            pass

    def head(self, bucket, key):
        blob = self.blob(bucket, key)
        try:
            blob.reload(timeout=60)
        except NotFound:
            return None
        except Exception:
            raise StorageFailure("provider_unavailable") from None
        return ObjectInfo(size=blob.size, crc32c=blob.crc32c, version=str(blob.generation))

    def delete(self, bucket, key, version):
        generation = int(version) if version is not None else None
        try:
            self.blob(bucket, key, generation).delete(if_generation_match=generation, timeout=60)
        except NotFound:
            pass
        except StorageFailure:
            raise
        except Exception:
            raise StorageFailure("provider_unavailable") from None

    def signed_download(self, bucket, key, version, filename, disposition, expires_in):
        generation = int(version) if version is not None else None
        blob = self.storage_client().bucket(bucket).blob(key, generation=generation)
        try:
            blob.reload(timeout=60)
        except NotFound:
            raise FileNotFoundError(key) from None
        content_disposition, content_type = download_headers(filename, blob.content_type, disposition)
        overrides = {"response_disposition": content_disposition}
        if content_type:
            overrides["response_type"] = content_type
        return blob.generate_signed_url(version="v4", expiration=expires_in, method="GET", generation=generation, **overrides, **signing(self.identity()))

    def list_keys(self, bucket, prefix):
        try:
            return [blob.name for blob in self.storage_client().bucket(bucket).list_blobs(prefix=prefix, timeout=30)]
        except Exception:
            raise StorageFailure("provider_unavailable") from None

    def put_bytes(self, bucket, key, data, content_type):
        try:
            self.storage_client().bucket(bucket).blob(key).upload_from_string(data, content_type=content_type, timeout=60)
        except Exception:
            raise StorageFailure("provider_unavailable") from None

    def get_text(self, bucket, key):
        blob = self.storage_client().bucket(bucket).blob(key)
        try:
            return blob.download_as_text(timeout=60)
        except NotFound:
            return None
        except Exception:
            raise StorageFailure("provider_unavailable") from None
