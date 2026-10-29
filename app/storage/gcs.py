import json
from functools import lru_cache
from urllib.parse import quote
from xml.etree import ElementTree as ET

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from google.api_core.exceptions import NotFound
from google.cloud.storage.retry import DEFAULT_RETRY

from app.config import get_secret
from app.services.storage_downloads import download_headers
from app.storage.base import ObjectInfo, Part, StorageFailure, UploadMissing, check_part_listing


def field(root, name):
    return root.findtext("{*}" + name)


@lru_cache
def key_credentials():
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_info(json.loads(get_secret("GCS_SA")))


@lru_cache
def key_client():
    return storage.Client(credentials=key_credentials())


class GoogleStore:
    """Google Cloud Storage. Multipart uploads go through the XML API, signed as MULTIPART_UPLOAD_SIGNER
    by impersonation, and are refused when the multipart settings are incomplete; downloads, retiring,
    and record files use the GCS_SA key, as the legacy routes do."""

    retires_by_lifecycle = True

    def __init__(self, config):
        self.config = config
        self.credentials = self.client = self.http = None
        if not all((config.bucket, config.signer_service_account, config.project)):
            return
        source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=config.signer_service_account,
            target_scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
            lifetime=900,
        )
        self.credentials = credentials
        self.client = storage.Client(project=config.project, credentials=credentials)
        self.http = AuthorizedSession(credentials)

    def blob(self, bucket, key, generation=None):
        self.check_bucket(bucket)
        return self.client.bucket(bucket).blob(key, generation=generation)

    def check_bucket(self, bucket):
        if self.http is None:
            raise StorageFailure("storage_not_configured")
        if bucket != self.config.bucket:
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
            url = self.blob(bucket, key).generate_signed_url(version="v4", expiration=expires_at, method="PUT", headers=headers, query_parameters={"uploadId": upload_id, "partNumber": str(number)}, credentials=self.credentials)
            return url, headers
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
        return ObjectInfo(size=blob.size, crc32c=blob.crc32c, version=str(blob.generation))

    def delete(self, bucket, key, version):
        generation = int(version) if version is not None else None
        try:
            self.blob(bucket, key, generation).delete(if_generation_match=generation, timeout=60)
        except NotFound:
            pass

    def signed_download(self, bucket, key, version, filename, disposition, expires_in):
        generation = int(version) if version is not None else None
        blob = key_client().bucket(bucket).blob(key, generation=generation)
        try:
            blob.reload()
        except NotFound:
            raise FileNotFoundError(key) from None
        content_disposition, content_type = download_headers(filename, blob.content_type, disposition)
        overrides = {"response_disposition": content_disposition}
        if content_type:
            overrides["response_type"] = content_type
        return blob.generate_signed_url(version="v4", expiration=expires_in, method="GET", generation=generation, **overrides)

    def retire(self, bucket, key, version, retired_at):
        blob = key_client().bucket(bucket).blob(key)
        if version is None:
            try:
                blob.reload(timeout=30, retry=DEFAULT_RETRY.with_timeout(30))
            except NotFound:
                return False
            version = blob.generation
        blob.custom_time = retired_at
        blob.patch(if_generation_match=int(version), timeout=30, retry=DEFAULT_RETRY.with_timeout(30))
        return True

    def put_bytes(self, bucket, key, data, content_type):
        key_client().bucket(bucket).blob(key).upload_from_string(data, content_type=content_type)

    def get_text(self, bucket, key):
        blob = key_client().bucket(bucket).blob(key)
        return blob.download_as_text() if blob.exists() else None
