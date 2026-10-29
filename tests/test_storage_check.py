from urllib.parse import parse_qs, urlparse

import pytest

import app.cli.storage as check
from app.cli.storage import StorageCheck, crc32c
from app.storage import ObjectInfo, Part

PART_BYTES = 64


class Store:
    """A store that checks signed part URLs the way S3 does: the checksum header must be the signed
    one (else 403) and must match the bytes (else 400). honours_signature=False lets a changed or
    missing header through, as a store that ignores the signature would."""

    def __init__(self, honours_signature=True, download_status=200):
        self.honours_signature = honours_signature
        self.download_status = download_status
        self.parts, self.objects, self.records, self.deleted = {}, {}, {}, []

    def start_upload(self, bucket, key):
        return "upload-1"

    def sign_part(self, bucket, key, upload_id, number, crc, size, expires_at):
        return f"https://store.example/{number}?crc={crc}", {"x-amz-checksum-crc32c": crc, "Content-Length": str(size)}

    def receive(self, url, data, headers):
        query = urlparse(url)
        signed = parse_qs(query.query)["crc"][0]
        sent = headers.get("x-amz-checksum-crc32c")
        if self.honours_signature and sent != signed:
            return 403
        if sent is not None and crc32c(data) != sent:
            return 400
        self.parts[int(query.path.strip("/"))] = data
        return 200

    def list_parts(self, bucket, key, upload_id, marker=0, limit=1000):
        return [Part(part_number=n, size=len(data), etag=f"e{n}") for n, data in sorted(self.parts.items())], None

    def finish_upload(self, bucket, key, upload_id, parts, crc32c_by_part):
        self.objects[key] = b"".join(self.parts[part.part_number] for part in parts)

    def abort_upload(self, bucket, key, upload_id):
        pass

    def head(self, bucket, key):
        return ObjectInfo(size=len(self.objects[key]), crc32c=None, version="v1") if key in self.objects else None

    def signed_download(self, bucket, key, version, filename, disposition, expires_in):
        return f"https://store.example/download/{key}"

    def put_bytes(self, bucket, key, data, content_type):
        self.records[key] = data

    def get_text(self, bucket, key):
        return self.records[key].decode()

    def delete(self, bucket, key, version):
        self.objects.pop(key, None)
        self.deleted.append(key)


@pytest.fixture
def run(monkeypatch):
    def run_check(store):
        def send(method, url, data=None, headers=None):
            if method == "PUT":
                return store.receive(url, data, headers or {}), b"", {}
            key = url.split("/download/", 1)[1]
            return store.download_status, store.objects.get(key, b""), {"Content-Disposition": 'attachment; filename="check file.bin"'}
        monkeypatch.setattr(check, "send", send)
        lines = []
        passed = StorageCheck(store, "files", "records", PART_BYTES, keep=False, out=lines.append).run()
        return passed, lines
    return run_check


def test_a_store_that_checks_signed_checksums_passes_and_is_left_clean(run):
    store = Store()
    passed, lines = run(store)
    assert passed, lines
    assert [line.split(":")[0] for line in lines if "refuse" in line] == [
        "ok      refuse a part with the wrong checksum",
        "ok      refuse other bytes with the checksum header changed to match them",
        "ok      refuse other bytes without the checksum header",
    ]
    assert store.objects == {}


def test_a_store_that_ignores_the_signed_checksum_fails(run):
    passed, lines = run(Store(honours_signature=False))
    assert not passed
    assert lines[-1].startswith("FAILED  refuse other bytes with the checksum header changed to match them")


def test_a_failure_after_completion_still_removes_the_test_file(run):
    store = Store(download_status=403)
    passed, lines = run(store)
    assert not passed
    assert lines[-1].startswith("FAILED  download through a signed URL")
    assert store.objects == {}
    assert len(store.deleted) == 1
