import argparse
import base64
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import google_crc32c

from app.storage.crc32c import combine_parts

PREFIX = "_crucible-check"
CHECKSUM_HEADERS = ("x-amz-checksum-crc32c", "x-goog-hash")


def crc32c(data: bytes) -> str:
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode()


def send(method: str, url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
    request = Request(url, data=data, method=method, headers={name: value for name, value in (headers or {}).items() if name.lower() != "content-length"})
    try:
        with urlopen(request, timeout=120) as response:
            return response.status, response.read(), dict(response.headers)
    except HTTPError as error:
        return error.code, error.read(), dict(error.headers)


class StorageCheck:
    """Uploads a small file the way clients do (signed part URLs and the headers the API returns),
    reads it back through a signed download, and removes it."""

    def __init__(self, store, bucket: str, record_bucket: str | None, part_bytes: int, keep: bool, out=print):
        self.store, self.bucket, self.record_bucket, self.keep, self.out = store, bucket, record_bucket, keep, out
        self.key = f"{PREFIX}/{uuid.uuid4().hex}"
        self.parts = {1: os.urandom(part_bytes), 2: os.urandom(1024)}
        self.data = self.parts[1] + self.parts[2]
        self.upload_id = None

    def put_part(self, number: int, crc: str) -> int:
        data = self.parts[number]
        url, headers = self.store.sign_part(self.bucket, self.key, self.upload_id, number, crc, len(data), datetime.now(timezone.utc) + timedelta(minutes=15))
        status, _, _ = send("PUT", url, data, headers)
        return status

    def put_other_bytes(self, header: str) -> int:
        """Send other bytes of part 2's length to the URL signed for part 2, with the checksum header
        changed to match them, or left out."""
        data = self.parts[2]
        other = os.urandom(len(data))
        url, headers = self.store.sign_part(self.bucket, self.key, self.upload_id, 2, crc32c(data), len(data), datetime.now(timezone.utc) + timedelta(minutes=15))
        name = next((name for name in headers if name.lower() in CHECKSUM_HEADERS), None)
        if name is None:
            raise AssertionError(f"the store signed no checksum header (headers: {sorted(headers)})")
        headers = dict(headers)
        if header == "changed":
            headers[name] = crc32c(other) if name.lower() == "x-amz-checksum-crc32c" else f"crc32c={crc32c(other)}"
        else:
            del headers[name]
        status, _, _ = send("PUT", url, other, headers)
        return status

    def steps(self):
        def start():
            self.upload_id = self.store.start_upload(self.bucket, self.key)
            return f"upload {self.key}"

        def part(number, again=False):
            def step():
                status = self.put_part(number, crc32c(self.parts[number]))
                if status != 200:
                    raise AssertionError(f"HTTP {status}")
                return f"{len(self.parts[number])} bytes" + (", replacing the first upload" if again else "")
            return step

        def wrong_checksum():
            status = self.put_part(2, crc32c(b"not this part"))
            if status < 400 or status >= 500:
                raise AssertionError(f"a part with the wrong checksum answered HTTP {status}")
            return f"refused with HTTP {status}"

        def other_bytes(header):
            def step():
                status = self.put_other_bytes(header)
                if status < 400 or status >= 500:
                    raise AssertionError(f"the store accepted other bytes with the checksum header {header} (HTTP {status})")
                return f"refused with HTTP {status}"
            return step

        def listed():
            parts, _ = self.store.list_parts(self.bucket, self.key, self.upload_id)
            found = [(item.part_number, item.size) for item in parts]
            if found != [(1, len(self.parts[1])), (2, len(self.parts[2]))]:
                raise AssertionError(f"listed {found}")
            mismatched = [item.part_number for item in parts if item.crc32c is not None and item.crc32c != crc32c(self.parts[item.part_number])]
            if mismatched:
                raise AssertionError(f"store reports other checksums for parts {mismatched}")
            self.listing = parts
            return "2 parts, " + ("checksums reported by the store and matching" if all(item.crc32c for item in parts) else "the store reports no part checksums")

        def finish():
            self.store.finish_upload(self.bucket, self.key, self.upload_id, self.listing, {number: crc32c(data) for number, data in self.parts.items()})
            return "completed"

        def head():
            info = self.store.head(self.bucket, self.key)
            if info is None or info.size != len(self.data):
                raise AssertionError(f"stored object is {info}")
            expected = crc32c(self.data)
            if info.crc32c is not None and info.crc32c != expected:
                raise AssertionError(f"store reports CRC32C {info.crc32c}, expected {expected}")
            combined = combine_parts([(crc32c(data), len(data)) for data in self.parts.values()])
            if combined != expected:
                raise AssertionError("combined part checksums do not match the object")
            self.version = info.version
            return f"{info.size} bytes, version {info.version}, " + ("CRC32C reported by the store" if info.crc32c else "CRC32C combined from the parts")

        def download():
            url = self.store.signed_download(self.bucket, self.key, self.version, "check file.bin", "attachment", timedelta(minutes=5))
            status, body, headers = send("GET", url)
            if status != 200 or body != self.data:
                raise AssertionError(f"HTTP {status}, {len(body)} bytes")
            disposition = {name.lower(): value for name, value in headers.items()}.get("content-disposition", "")
            if 'filename="check file.bin"' not in disposition:
                raise AssertionError(f"Content-Disposition is {disposition!r}")
            return "bytes and file name match"

        def records():
            key = f"{PREFIX}/record.json"
            payload = f'{{"checked_at": "{datetime.now(timezone.utc).isoformat()}"}}'
            self.store.put_bytes(self.record_bucket, key, payload.encode(), "application/json")
            if self.store.get_text(self.record_bucket, key) != payload:
                raise AssertionError("read back different content")
            return f"wrote and read {self.record_bucket}/{key}"

        def delete():
            self.store.delete(self.bucket, self.key, self.version)
            if self.store.head(self.bucket, self.key) is not None:
                raise AssertionError("object still present")
            return "removed"

        yield "start a multipart upload", start
        yield "upload part 1 through its signed URL", part(1)
        yield "upload part 1 again", part(1, again=True)
        yield "refuse a part with the wrong checksum", wrong_checksum
        yield "refuse other bytes with the checksum header changed to match them", other_bytes("changed")
        yield "refuse other bytes without the checksum header", other_bytes("left out")
        yield "upload part 2", part(2)
        yield "list the parts", listed
        yield "finish the upload", finish
        yield "read the object's size and checksum", head
        yield "download through a signed URL", download
        if self.record_bucket:
            yield "record files", records
        if not self.keep:
            yield "delete the object", delete

    def run(self) -> bool:
        try:
            for name, step in self.steps():
                try:
                    self.out(f"ok      {name}: {step()}")
                except Exception as error:
                    self.out(f"FAILED  {name}: {error or type(error).__name__}")
                    return False
            return True
        finally:
            self.clean_up()

    def clean_up(self):
        if self.keep or not self.upload_id:
            return
        try:
            self.store.abort_upload(self.bucket, self.key, self.upload_id)
            if self.store.head(self.bucket, self.key) is not None:
                self.store.delete(self.bucket, self.key, None)
        except Exception as error:
            self.out(f"warning: could not remove {self.bucket}/{self.key}: {error or type(error).__name__}")


def check_store(public: bool):
    from app.config import get_settings
    from app.storage import get_store

    settings = get_settings().storage
    if settings.backend == "s3" and not public:
        from app.storage.s3 import S3Store

        return S3Store(settings.model_copy(update={"s3_public_url": None}))
    return get_store()


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("storage", help="Check file storage").add_subparsers(dest="command", required=True)
    check = commands.add_parser(
        "check",
        help="Upload, download, and delete a test file with the configured storage",
        description="Runs a two-part signed upload into UPLOAD_BUCKET under _crucible-check/, re-uploads a part, checks that the store refuses a part whose bytes do not match the signed checksum (sent with the signed checksum, with the checksum header changed to match the bytes, and without it), finishes the upload, downloads the file through a signed URL, writes and reads _crucible-check/record.json in RECORD_BUCKET when it is set (left there and overwritten by the next run), and deletes the test file, also when a step fails. Signed URLs use S3_ENDPOINT_URL unless --public is given. Exits 1 when a step fails.",
    )
    check.add_argument("--size-mib", type=int, default=5, help="Size of the first part in MiB (S3 requires at least 5; default 5)")
    check.add_argument("--keep", action="store_true", help="Leave the test file in the bucket")
    check.add_argument("--public", action="store_true", help="Sign URLs for S3_PUBLIC_URL, as clients receive them (run from where clients run)")
    check.set_defaults(handler=_run_check)
    cleanup = commands.add_parser(
        "cleanup",
        help="Abort expired uploads and delete file versions past their grace period",
        description="Aborts multipart uploads that expired or whose cleanup failed, and deletes the stored content of file versions older than FILE_VERSION_RETENTION_DAYS that no file uses. Run it hourly. Does nothing unless STORAGE_CLEANUP_ENABLED=true, except --dry-run. Exits 1 when anything failed.",
    )
    cleanup.add_argument("--dry-run", action="store_true", help="Report what would be done without changing anything")
    cleanup.add_argument("--batch", type=int, default=500, help="Most uploads and versions to handle in one run (default 500)")
    cleanup.set_defaults(handler=_run_cleanup)


def _run_check(args: argparse.Namespace) -> int:
    from app.config import ConfigurationError, check_settings, get_settings

    try:
        check_settings()
        settings = get_settings().storage
        if not settings.upload_bucket:
            raise ConfigurationError("UPLOAD_BUCKET is not set")
        if settings.backend == "gcs":
            from app.utils.multipart_config import MultipartStorageConfig

            config = MultipartStorageConfig.from_env()
            if not config.bucket:
                raise ConfigurationError("checking Google Cloud Storage needs MULTIPART_UPLOAD_BUCKET")
        store = check_store(args.public)
    except Exception as error:
        print(f"Storage is not usable: {error}", file=sys.stderr)
        return 1
    where = f" at {settings.s3_public_url if args.public and settings.s3_public_url else settings.s3_endpoint_url}" if settings.backend == "s3" else ""
    print(f"Checking {settings.backend} storage{where}, bucket {settings.upload_bucket}")
    passed = StorageCheck(store, settings.upload_bucket, settings.record_bucket, args.size_mib * 1024 * 1024, args.keep).run()
    print("Storage check passed" if passed else "Storage check failed")
    return 0 if passed else 1


def _run_cleanup(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.config import check_settings, get_settings
    from app.services.multipart import now
    from app.services.storage_cleanup import run_cleanup
    from app.storage import get_store

    try:
        check_settings()
        if not get_settings().storage.cleanup_enabled and not args.dry_run:
            print("storage cleanup is disabled (STORAGE_CLEANUP_ENABLED=false); nothing done")
            return 0
        store = get_store()
    except Exception as error:
        print(f"Storage is not usable: {error}", file=sys.stderr)
        return 1
    report = run_cleanup(get_engine(), store, now(), dry_run=args.dry_run, batch=args.batch)
    verb = "would " if args.dry_run else ""
    print(f"{verb}abort {report.aborted_uploads} uploads, {verb}delete {report.deleted_versions} file versions; {report.kept_in_use} still in use, {len(report.failures)} failures")
    for failure in report.failures:
        print(f"  failed: {failure}")
    return 1 if report.failures else 0
