from contextlib import contextmanager
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_secret
from app.services.storage_downloads import download_headers
from app.storage.base import ObjectInfo, Part, StorageFailure, UploadMissing, check_part_listing

MISSING_OBJECT = {"404", "NoSuchKey", "NotFound"}


def error_code(error: ClientError) -> str | None:
    return error.response.get("Error", {}).get("Code")


@contextmanager
def failures(code="provider_request_failed"):
    try:
        yield
    except ClientError as error:
        if error_code(error) == "NoSuchUpload":
            raise UploadMissing("upload_missing") from None
        raise StorageFailure(code) from None
    except BotoCoreError:
        raise StorageFailure("provider_unavailable") from None


def find(operation, **params):
    """The operation's response, or None when the object does not exist."""
    try:
        return operation(**params)
    except ClientError as error:
        if error_code(error) in MISSING_OBJECT:
            return None
        raise StorageFailure("provider_request_failed") from None
    except BotoCoreError:
        raise StorageFailure("provider_unavailable") from None


class S3Store:
    """An S3-compatible store through boto3. Requests go to S3_ENDPOINT_URL; presigned URLs carry
    S3_PUBLIC_URL, the address clients reach the store on. Keys are never overwritten, so versions
    are informational: the VersionId when the bucket keeps versions, else the ETag."""

    retires_by_lifecycle = False

    def __init__(self, settings):
        if not settings.s3_endpoint_url:
            raise StorageFailure("storage_not_configured")
        # boto3 1.36 and later add streaming checksums by default, which several S3 servers reject.
        config = Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            connect_timeout=10,
            read_timeout=240,
            retries={"mode": "standard", "max_attempts": 3},
        )
        session = boto3.session.Session(
            aws_access_key_id=get_secret("S3_ACCESS_KEY_ID"),
            aws_secret_access_key=get_secret("S3_SECRET_ACCESS_KEY"),
            region_name=settings.s3_region,
        )
        self.client = session.client("s3", endpoint_url=settings.s3_endpoint_url, config=config)
        self.presigner = session.client("s3", endpoint_url=settings.s3_public_url or settings.s3_endpoint_url, config=config)

    def start_upload(self, bucket, key):
        with failures():
            response = self.client.create_multipart_upload(Bucket=bucket, Key=key, ContentType="application/octet-stream", ChecksumAlgorithm="CRC32C")
        if not response.get("UploadId"):
            raise StorageFailure("missing_provider_upload_id")
        return response["UploadId"]

    def open_uploads(self, bucket, key):
        with failures():
            pages = self.client.get_paginator("list_multipart_uploads").paginate(Bucket=bucket, Prefix=key)
            return [upload["UploadId"] for page in pages for upload in page.get("Uploads", []) if upload.get("Key") == key]

    def list_parts(self, bucket, key, upload_id, marker=0, limit=1000):
        with failures():
            response = self.client.list_parts(Bucket=bucket, Key=key, UploadId=upload_id, MaxParts=limit, PartNumberMarker=marker)
        try:
            parts = [Part(part_number=int(part["PartNumber"]), size=int(part["Size"]), etag=part.get("ETag", ""), crc32c=part.get("ChecksumCRC32C")) for part in response.get("Parts", [])]
            next_marker = int(response["NextPartNumberMarker"]) if response.get("IsTruncated") else None
        except (KeyError, TypeError, ValueError):
            raise StorageFailure("invalid_part_listing") from None
        # SeaweedFS 4.51 lists a part uploaded twice twice; identical entries are the same part.
        parts = list(dict.fromkeys(parts))
        check_part_listing(parts, next_marker, marker, limit)
        return parts, next_marker

    def sign_part(self, bucket, key, upload_id, number, crc32c, size, expires_at):
        expires_in = max(1, int((expires_at - datetime.now(timezone.utc)).total_seconds()))
        params = {"Bucket": bucket, "Key": key, "UploadId": upload_id, "PartNumber": number, "ChecksumCRC32C": crc32c, "ContentLength": size}
        with failures("signing_failed"):
            url = self.presigner.generate_presigned_url("upload_part", Params=params, ExpiresIn=expires_in)
        return url, {"x-amz-checksum-crc32c": crc32c, "Content-Length": str(size)}

    def finish_upload(self, bucket, key, upload_id, parts, crc32c_by_part):
        listed = [{"PartNumber": part.part_number, "ETag": part.etag, "ChecksumCRC32C": crc32c_by_part[part.part_number]} for part in parts]
        with failures():
            self.client.complete_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id, MultipartUpload={"Parts": listed})

    def abort_upload(self, bucket, key, upload_id):
        try:
            with failures():
                self.client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
        except UploadMissing:
            pass

    def head(self, bucket, key):
        response = find(self.client.head_object, Bucket=bucket, Key=key)
        if response is None:
            return None
        version = response.get("VersionId")
        if not version or version == "null":
            version = response.get("ETag", "").strip('"')
        return ObjectInfo(size=int(response["ContentLength"]), crc32c=None, version=version)

    def delete(self, bucket, key, version):
        with failures():
            self.client.delete_object(Bucket=bucket, Key=key)

    def signed_download(self, bucket, key, version, filename, disposition, expires_in):
        response = find(self.client.head_object, Bucket=bucket, Key=key)
        if response is None:
            raise FileNotFoundError(key)
        content_disposition, content_type = download_headers(filename, response.get("ContentType"), disposition)
        params = {"Bucket": bucket, "Key": key, "ResponseContentDisposition": content_disposition}
        if content_type:
            params["ResponseContentType"] = content_type
        with failures("signing_failed"):
            return self.presigner.generate_presigned_url("get_object", Params=params, ExpiresIn=int(expires_in.total_seconds()))

    def retire(self, bucket, key, version, retired_at):
        return False

    def put_bytes(self, bucket, key, data, content_type):
        with failures():
            self.client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)

    def get_text(self, bucket, key):
        response = find(self.client.get_object, Bucket=bucket, Key=key)
        if response is None:
            return None
        with failures():
            return response["Body"].read().decode()
