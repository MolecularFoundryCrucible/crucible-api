import hashlib
from functools import lru_cache
from urllib.parse import quote
from xml.etree import ElementTree as ET

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
from google.api_core.exceptions import NotFound

from app.utils.multipart_config import MultipartStorageConfig


class StorageFailure(Exception):
    pass


class UploadMissing(StorageFailure):
    pass


def field(root, name):
    return root.findtext("{*}" + name)


class MultipartStorage:
    def __init__(self, config):
        if not all((config.bucket, config.signer_service_account, config.project)):
            raise StorageFailure("storage_not_configured")
        source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=config.signer_service_account,
            target_scopes=["https://www.googleapis.com/auth/devstorage.read_write"],
            lifetime=900,
        )
        self.config = config
        self.credentials = credentials
        self.client = storage.Client(project=config.project, credentials=credentials)
        self.http = AuthorizedSession(credentials)

    def blob(self, upload, generation=None):
        self.check_bucket(upload.bucket)
        return self.client.bucket(upload.bucket).blob(upload.gcs_path, generation=generation)

    def check_bucket(self, bucket):
        if bucket != self.config.bucket:
            raise StorageFailure("bucket_mismatch")

    def request(self, method, upload, *, bucket_only=False, **kwargs):
        self.check_bucket(upload.bucket)
        url = f"https://storage.googleapis.com/{quote(upload.bucket, safe='')}"
        url += "/" if bucket_only else "/" + quote(upload.gcs_path, safe="/")
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

    def initiate(self, upload):
        root = self.request("POST", upload, params={"uploads": ""}, data=b"", headers={"Content-Type": "application/octet-stream"})
        identifier = field(root, "UploadId")
        if not identifier:
            raise StorageFailure("missing_provider_upload_id")
        return identifier

    def uploads(self, upload):
        params = {"uploads": "", "prefix": upload.gcs_path, "max-uploads": "1000"}
        identifiers = []
        seen = set()
        for _ in range(100):
            root = self.request("GET", upload, bucket_only=True, params=params)
            identifiers.extend(field(item, "UploadId") for item in root.findall("{*}Upload") if field(item, "Key") == upload.gcs_path)
            if field(root, "IsTruncated") != "true":
                return identifiers
            marker = (field(root, "NextKeyMarker"), field(root, "NextUploadIdMarker"))
            if not all(marker) or marker in seen:
                break
            seen.add(marker)
            params.update({"key-marker": marker[0], "upload-id-marker": marker[1]})
        raise StorageFailure("invalid_upload_pagination")

    def list_parts(self, upload, marker=0, limit=1000):
        root = self.request("GET", upload, params={"uploadId": upload.provider_upload_id, "max-parts": str(limit), "part-number-marker": str(marker)})
        try:
            parts = [{"part_number": int(field(part, "PartNumber")), "size": int(field(part, "Size")), "etag": field(part, "ETag")} for part in root.findall("{*}Part")]
            next_marker = int(field(root, "NextPartNumberMarker")) if field(root, "IsTruncated") == "true" else None
            numbers = [part["part_number"] for part in parts]
            if len(parts) > limit or numbers != sorted(set(numbers)) or any(n <= marker or n > 10000 for n in numbers):
                raise ValueError()
            if next_marker is not None and (not parts or next_marker != numbers[-1] or next_marker <= marker):
                raise ValueError()
            if any(not part["etag"] or part["size"] < 1 for part in parts):
                raise ValueError()
            return parts, next_marker
        except (TypeError, ValueError):
            raise StorageFailure("invalid_part_listing") from None

    def sign_part(self, upload, number, checksum, size, expires_at):
        headers = {"x-goog-hash": f"crc32c={checksum}", "Content-Length": str(size)}
        try:
            url = self.blob(upload).generate_signed_url(version="v4", expiration=expires_at, method="PUT", headers=headers, query_parameters={"uploadId": upload.provider_upload_id, "partNumber": str(number)}, credentials=self.credentials)
            return url, headers
        except Exception:
            raise StorageFailure("signing_failed") from None

    def complete(self, upload, parts):
        root = ET.Element("CompleteMultipartUpload")
        for item in parts:
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(item["part_number"])
            ET.SubElement(part, "ETag").text = item["etag"]
        self.request("POST", upload, params={"uploadId": upload.provider_upload_id}, data=ET.tostring(root), headers={"Content-Type": "application/xml"})

    def abort(self, upload, provider_id):
        try:
            self.request("DELETE", upload, params={"uploadId": provider_id})
        except UploadMissing:
            pass

    def metadata(self, upload):
        blob = self.blob(upload)
        try:
            blob.reload(timeout=60)
        except NotFound:
            return None
        return {"generation": str(blob.generation), "size": blob.size, "crc32c": blob.crc32c}

    def sha256(self, upload, generation, heartbeat):
        blob = self.blob(upload, int(generation))
        digest = hashlib.sha256()
        with blob.open("rb", chunk_size=8 * 1024 ** 2, timeout=60, if_generation_match=int(generation)) as stream:
            while True:
                heartbeat()
                chunk = stream.read(8 * 1024 ** 2)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def delete(self, upload, generation):
        try:
            self.blob(upload, int(generation)).delete(if_generation_match=int(generation), timeout=60)
        except NotFound:
            pass


@lru_cache
def get_multipart_storage():
    return MultipartStorage(MultipartStorageConfig.from_env())
