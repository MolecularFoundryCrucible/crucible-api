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
        self.finished = False

    def put_part(self, number: int, crc: str) -> int:
        data = self.parts[number]
        url, headers = self.store.sign_part(self.bucket, self.key, self.upload_id, number, crc, len(data), datetime.now(timezone.utc) + timedelta(minutes=15))
        status, _, _ = send("PUT", url, data, headers)
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
            self.finished = True
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
            self.finished = False
            return "removed"

        yield "start a multipart upload", start
        yield "upload part 1 through its signed URL", part(1)
        yield "upload part 1 again", part(1, again=True)
        yield "refuse a part with the wrong checksum", wrong_checksum
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
        try:
            if self.upload_id and not self.finished and not self.keep:
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
        description="Runs a two-part signed upload into UPLOAD_BUCKET under _crucible-check/, re-uploads a part, checks that a part with the wrong checksum is refused, finishes the upload, downloads the file through a signed URL, writes and reads a record file in RECORD_BUCKET when it is set, and deletes the test file. Signed URLs use S3_ENDPOINT_URL unless --public is given. Exits 1 when a step fails.",
    )
    check.add_argument("--size-mib", type=int, default=5, help="Size of the first part in MiB (S3 requires at least 5; default 5)")
    check.add_argument("--keep", action="store_true", help="Leave the test file in the bucket")
    check.add_argument("--public", action="store_true", help="Sign URLs for S3_PUBLIC_URL, as clients receive them (run from where clients run)")
    check.set_defaults(handler=_run_check)


def _run_check(args: argparse.Namespace) -> int:
    from app.config import ConfigurationError, check_settings, get_settings

    try:
        check_settings()
        settings = get_settings().storage
        if not settings.upload_bucket:
            raise ConfigurationError("UPLOAD_BUCKET is not set")
        store = check_store(args.public)
    except Exception as error:
        print(f"Storage is not usable: {error}", file=sys.stderr)
        return 1
    where = f" at {settings.s3_public_url if args.public and settings.s3_public_url else settings.s3_endpoint_url}" if settings.backend == "s3" else ""
    print(f"Checking {settings.backend} storage{where}, bucket {settings.upload_bucket}")
    passed = StorageCheck(store, settings.upload_bucket, settings.record_bucket, args.size_mib * 1024 * 1024, args.keep).run()
    print("Storage check passed" if passed else "Storage check failed")
    return 0 if passed else 1
