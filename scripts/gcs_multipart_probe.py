"""Exercise signed XML multipart uploads using disposable, isolated GCS buckets."""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sys
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import google.auth
from google.auth import impersonated_credentials
from google.auth.transport.requests import AuthorizedSession
from google.cloud import storage
import google_crc32c
from google.oauth2 import service_account
from dotenv import dotenv_values
import requests

SCOPE = "https://www.googleapis.com/auth/devstorage.read_write"
BUCKET_PREFIX = "mf-crucible-mpu-stage1-"
TIMEOUT = (15, 90)


def crc32c(data):
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode("ascii")


def field(root, name):
    return root.findtext(f"{{*}}{name}")


def error_code(response):
    try:
        value = field(ET.fromstring(response.content), "Code")
        return value if value and re.fullmatch(r"[A-Za-z0-9]+", value) else None
    except ET.ParseError:
        return None


def change_query(url, **changes):
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(changes)
    return urlunsplit(parts._replace(query=urlencode(query)))


class ProbeFailure(Exception):
    pass


class Probe:
    def __init__(self, args):
        self.args = args
        source, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        credentials = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=args.signer,
            target_scopes=[SCOPE],
            lifetime=900,
        )
        self.credentials = credentials
        self.server = AuthorizedSession(credentials)
        self.client = storage.Client(project=args.project, credentials=credentials)
        self.prefix = f"mpu-stage1/{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(6)}/"
        self.uploads = []
        self.objects = set()
        self.results = []

    def record(self, name, passed, **evidence):
        self.results.append({"check": name, "passed": bool(passed), **evidence})

    def require(self, name, condition, **evidence):
        self.record(name, condition, **evidence)
        if not condition:
            raise ProbeFailure(name)

    def endpoint(self, bucket, name):
        return f"https://storage.googleapis.com/{bucket}/{quote(name, safe='/')}"

    def request(self, method, url, **kwargs):
        return self.server.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs)

    def initiate(self, name):
        url = self.endpoint(self.args.bucket, name)
        response = self.request("POST", url, params={"uploads": ""}, data=b"", headers={"Content-Type": "application/octet-stream"})
        self.require("provider_initiate", response.status_code == 200, http_status=response.status_code, provider_code=error_code(response))
        upload_id = field(ET.fromstring(response.content), "UploadId")
        if not upload_id:
            raise ProbeFailure("missing_provider_upload_id")
        self.uploads.append((name, upload_id))
        return upload_id

    def signed_part(self, name, upload_id, number, data, lifetime=600):
        headers = {"x-goog-hash": f"crc32c={crc32c(data)}", "Content-Length": str(len(data))}
        url = self.client.bucket(self.args.bucket).blob(name).generate_signed_url(
            version="v4",
            expiration=timedelta(seconds=lifetime),
            method="PUT",
            headers=headers,
            query_parameters={"uploadId": upload_id, "partNumber": str(number)},
            credentials=self.credentials,
        )
        return url, headers

    def anonymous(self, method, url, **kwargs):
        return requests.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs)

    def denied(self, label, method, url, data, headers):
        response = self.anonymous(method, url, data=data, headers=headers)
        self.record(label, response.status_code == 403, http_status=response.status_code, provider_code=error_code(response))

    def list_parts(self, name, upload_id):
        parts, marker, pages = [], "0", 0
        while True:
            response = self.request("GET", self.endpoint(self.args.bucket, name), params={"uploadId": upload_id, "max-parts": "1", "part-number-marker": marker})
            self.require("provider_list_parts", response.status_code == 200, http_status=response.status_code)
            root = ET.fromstring(response.content)
            for part in root.findall("{*}Part"):
                parts.append((int(field(part, "PartNumber")), field(part, "ETag"), int(field(part, "Size"))))
            pages += 1
            if field(root, "IsTruncated") != "true":
                break
            next_marker = field(root, "NextPartNumberMarker")
            if not next_marker or int(next_marker) <= int(marker) or pages >= 10:
                raise ProbeFailure("invalid_pagination")
            marker = next_marker
        self.record("paginated_part_listing", pages == 2 and len(parts) == 2, pages=pages, parts=len(parts))
        return parts

    def legacy_checks(self, name):
        info = json.loads(dotenv_values(self.args.legacy_env)["GCS_SA"])
        credentials = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
        self.record("legacy_identity_loaded", True, service_account=credentials.service_account_email)
        legacy = AuthorizedSession(credentials)
        upload_name = self.prefix + "legacy-create"
        self.objects.add((self.args.bucket, upload_name))
        target = self.endpoint(self.args.bucket, name)
        checks = [
            ("legacy_list_denied", "GET", f"https://storage.googleapis.com/{self.args.bucket}", {"params": {"prefix": self.prefix}}),
            ("legacy_read_denied", "GET", target, {}),
            ("legacy_create_denied", "PUT", self.endpoint(self.args.bucket, upload_name), {"data": b"probe"}),
            ("legacy_overwrite_denied", "PUT", target, {"data": b"probe"}),
            ("legacy_delete_denied", "DELETE", target, {}),
            ("legacy_multipart_initiate_denied", "POST", target, {"params": {"uploads": ""}, "data": b""}),
        ]
        for label, method, url, kwargs in checks:
            response = legacy.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs)
            self.record(label, response.status_code == 403, http_status=response.status_code, provider_code=error_code(response))
            if label == "legacy_multipart_initiate_denied" and response.status_code == 200:
                upload_id = field(ET.fromstring(response.content), "UploadId")
                if upload_id:
                    self.uploads.append((name, upload_id))
        legacy.close()

    def run(self):
        name = self.prefix + "complete"
        data = [b"a" * (5 * 1024**2), b"b" * (1024**2)]
        upload_id = self.initiate(name)
        signed = [self.signed_part(name, upload_id, i + 1, part) for i, part in enumerate(data)]
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.anonymous, "PUT", url, data=part, headers=headers) for (url, headers), part in zip(signed, data)]
            responses = [future.result() for future in futures]
        self.require("parallel_signed_upload", all(r.status_code == 200 for r in responses), http_statuses=[r.status_code for r in responses], bytes=sum(map(len, data)), elapsed_seconds=round(time.monotonic()-started, 3))
        url, headers = signed[1]
        response = self.anonymous("PUT", url, data=data[1], headers=headers)
        self.require("retry_same_part", response.status_code == 200, http_status=response.status_code)
        renewed_url, renewed_headers = self.signed_part(name, upload_id, 2, data[1])
        response = self.anonymous("PUT", renewed_url, data=data[1], headers=renewed_headers)
        self.require("renew_signed_part", response.status_code == 200, http_status=response.status_code)
        self.denied("changed_part_denied", "PUT", change_query(url, partNumber="3"), data[1], headers)
        self.denied("changed_upload_id_denied", "PUT", change_query(url, uploadId="invalid-probe-upload"), data[1], headers)
        parts = urlsplit(url)
        other_path = parts.path.rsplit("/", 1)[0] + "/different-object"
        self.denied("changed_object_denied", "PUT", urlunsplit(parts._replace(path=other_path)), data[1], headers)
        other_bucket_path = parts.path.replace(f"/{self.args.bucket}/", f"/{self.args.other_bucket}/", 1)
        self.denied("changed_bucket_denied", "PUT", urlunsplit(parts._replace(path=other_bucket_path)), data[1], headers)
        for method in ("POST", "DELETE"):
            self.denied(f"changed_method_{method.lower()}_denied", method, url, data[1], headers)
        get_url, get_headers = self.signed_part(name, upload_id, 2, b"")
        self.denied("changed_method_get_denied", "GET", get_url, b"", get_headers)
        self.denied("changed_checksum_header_denied", "PUT", url, data[1], {**headers, "x-goog-hash": "crc32c=AAAAAA=="})
        self.denied("changed_length_denied", "PUT", url, data[1] + b"x", headers)
        response = self.anonymous("PUT", url, data=b"c" * len(data[1]), headers=headers)
        self.record("corrupt_part_rejected", response.status_code == 400 and error_code(response) == "BadDigest", http_status=response.status_code, provider_code=error_code(response))
        expired_url, expired_headers = self.signed_part(name, upload_id, 2, data[1], lifetime=1)
        time.sleep(2)
        response = self.anonymous("PUT", expired_url, data=data[1], headers=expired_headers)
        self.record("expired_url_denied", response.status_code in (400, 403) and error_code(response) == "ExpiredToken", http_status=response.status_code, provider_code=error_code(response))
        parts = self.list_parts(name, upload_id)
        self.require("part_sizes_match", [p[2] for p in parts] == list(map(len, data)))
        root = ET.Element("CompleteMultipartUpload")
        for number, etag, _ in parts:
            part = ET.SubElement(root, "Part")
            ET.SubElement(part, "PartNumber").text = str(number)
            ET.SubElement(part, "ETag").text = etag
        self.objects.add((self.args.bucket, name))
        response = self.request("POST", self.endpoint(self.args.bucket, name), params={"uploadId": upload_id}, data=ET.tostring(root), headers={"Content-Type": "application/xml"})
        self.require("provider_complete", response.status_code == 200 and ET.fromstring(response.content).tag.endswith("CompleteMultipartUploadResult"), http_status=response.status_code, provider_code=error_code(response))
        blob = self.client.bucket(self.args.bucket).blob(name)
        blob.reload(timeout=TIMEOUT)
        expected = b"".join(data)
        self.require("final_size_crc32c", blob.size == len(expected) and blob.crc32c == crc32c(expected), bytes=blob.size, generation=str(blob.generation))
        started = time.monotonic()
        downloaded = self.client.bucket(self.args.bucket).blob(name, generation=blob.generation).download_as_bytes(timeout=TIMEOUT)
        self.require("generation_pinned_sha256", hashlib.sha256(downloaded).digest() == hashlib.sha256(expected).digest(), bytes=len(downloaded), elapsed_seconds=round(time.monotonic()-started, 3))
        response = self.anonymous("PUT", url, data=data[1], headers=headers)
        self.record("completed_upload_rejects_part", response.status_code == 404 and error_code(response) == "NoSuchUpload", http_status=response.status_code, provider_code=error_code(response))
        self.legacy_checks(name)
        abort_name = self.prefix + "abort"
        abort_id = self.initiate(abort_name)
        abort_url, abort_headers = self.signed_part(abort_name, abort_id, 1, b"abort-probe")
        response = self.anonymous("PUT", abort_url, data=b"abort-probe", headers=abort_headers)
        self.require("abort_probe_uploaded", response.status_code == 200, http_status=response.status_code)
        response = self.request("DELETE", self.endpoint(self.args.bucket, abort_name), params={"uploadId": abort_id})
        self.require("provider_abort", response.status_code == 204, http_status=response.status_code)
        response = self.anonymous("PUT", abort_url, data=b"abort-probe", headers=abort_headers)
        self.record("aborted_upload_rejects_part", response.status_code == 404 and error_code(response) == "NoSuchUpload", http_status=response.status_code, provider_code=error_code(response))

    def cleanup(self):
        for name, upload_id in self.uploads:
            try:
                response = self.request("DELETE", self.endpoint(self.args.bucket, name), params={"uploadId": upload_id})
                self.record("cleanup_provider_upload", response.status_code in (204, 404), http_status=response.status_code)
            except Exception as exc:
                self.record("cleanup_provider_upload", False, error_type=type(exc).__name__)
        for bucket, name in self.objects:
            try:
                blob = self.client.bucket(bucket).blob(name)
                if blob.exists(timeout=TIMEOUT):
                    blob.reload(timeout=TIMEOUT)
                    blob.delete(if_generation_match=blob.generation, timeout=TIMEOUT)
                self.record("cleanup_probe_object", True)
            except Exception as exc:
                self.record("cleanup_probe_object", False, error_type=type(exc).__name__)
        self.server.close()
        self.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="mf-crucible")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--other-bucket", required=True)
    parser.add_argument("--signer", required=True)
    parser.add_argument("--legacy-env", default=".env")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run", action="store_true", help="Run synthetic GCS writes and clean up this run's objects and uploads")
    args = parser.parse_args()
    if args.project != "mf-crucible" or not all(re.fullmatch(BUCKET_PREFIX + r"[a-z0-9-]+", bucket) for bucket in (args.bucket, args.other_bucket)):
        parser.error("Only explicitly named mf-crucible-mpu-stage1-* experiment buckets in mf-crucible are allowed")
    if args.bucket == args.other_bucket:
        parser.error("The alternate bucket must be distinct")
    if not re.fullmatch(r"mpu-stage1-[a-z0-9-]+@mf-crucible\.iam\.gserviceaccount\.com", args.signer):
        parser.error("Only a dedicated mpu-stage1-* signer is allowed")
    if not args.run:
        print(json.dumps({"mode": "plan", "bucket": args.bucket, "other_bucket": args.other_bucket, "signer": args.signer, "synthetic_payload_bytes": 6 * 1024**2, "cleanup": "only uploads and objects created by this run", "live_requests": False}, indent=2))
        return 0
    probe = Probe(args)
    try:
        probe.run()
    except Exception as exc:
        probe.record("probe_execution", False, error_type=type(exc).__name__, failed_check=str(exc) if isinstance(exc, ProbeFailure) else "redacted")
    finally:
        probe.cleanup()
    report = {"recorded_at": datetime.now(timezone.utc).isoformat(), "bucket": args.bucket, "other_bucket": args.other_bucket, "signer": args.signer, "prefix": probe.prefix, "passed": all(item["passed"] for item in probe.results), "checks": probe.results}
    payload = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload)
    print(payload)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
