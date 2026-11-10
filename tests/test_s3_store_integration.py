"""Uploads and downloads against real S3 servers. Skipped unless S3_TEST_ENDPOINTS names them
(comma-separated URLs), with S3_TEST_ACCESS_KEY and S3_TEST_SECRET_KEY valid on each."""

import base64
import hashlib
import os
from urllib.parse import urlparse

import google_crc32c
import pytest
from sqlmodel import Session

from app import storage
from app.cli.storage import StorageCheck, send
from app.tables import AccessGroup, Dataset, Resource, ResourceACL, User, UserAccessGroupLink
from app.services.multipart import MIN_PART_SIZE
from app.storage.s3 import S3Store
from app.utils.permissions import Role

ENDPOINTS = [url.strip() for url in os.environ.get("S3_TEST_ENDPOINTS", "").split(",") if url.strip()]
BUCKETS = ("crucible-files", "crucible-records")
BASE = "/datasets/ds/upload/multipart"

pytestmark = pytest.mark.skipif(not ENDPOINTS, reason="S3_TEST_ENDPOINTS is not set")


def crc32c(data):
    return base64.b64encode(google_crc32c.Checksum(data).digest()).decode()


@pytest.fixture(params=ENDPOINTS, ids=[urlparse(url).netloc for url in ENDPOINTS])
def store(request, monkeypatch):
    from app.config import get_settings

    for name, value in {
        "STORAGE_BACKEND": "s3", "S3_ENDPOINT_URL": request.param,
        "S3_ACCESS_KEY_ID": os.environ["S3_TEST_ACCESS_KEY"], "S3_SECRET_ACCESS_KEY": os.environ["S3_TEST_SECRET_KEY"],
        "UPLOAD_BUCKET": BUCKETS[0], "RECORD_BUCKET": BUCKETS[1], "MULTIPART_UPLOAD_ENABLED": "true", "MULTIPART_UPLOAD_BUCKET": BUCKETS[0],
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    storage.get_store.cache_clear()
    store = storage.get_store()
    assert isinstance(store, S3Store)
    existing = {bucket["Name"] for bucket in store.client.list_buckets()["Buckets"]}
    for bucket in set(BUCKETS) - existing:
        store.client.create_bucket(Bucket=bucket)
    yield store
    storage.get_store.cache_clear()


@pytest.fixture
def api(store, db_engine, client, auth_as):
    with Session(db_engine) as session:
        user = User(unique_id="owner", first_name="A", last_name="B", email="a@example.test")
        group = AccessGroup(group_name="owner-group")
        session.add_all([user, group, Resource(mfid="ds", resource_type="dataset"), Dataset(unique_id="ds", dataset_name="data")])
        session.commit()
        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=group.id, role="owner"))
        session.add(ResourceACL(mfid="ds", accessgroup_id=group.id, read=True, write=True, permission="owner"))
        session.commit()
        group_id = group.id
    auth_as("owner", groups=[("owner-group", group_id, Role.OWNER)])
    return client


def test_storage_check_passes(store):
    lines = []
    assert StorageCheck(store, BUCKETS[0], BUCKETS[1], 5 * 1024 * 1024, keep=False, out=lines.append).run(), "\n".join(lines)


@pytest.mark.parametrize("size, again", [(1, False), (2 * MIN_PART_SIZE, True), (MIN_PART_SIZE + 5, False)], ids=["one-byte", "exactly-two-parts-one-sent-twice", "short-last-part"])
def test_upload_through_the_api_then_download(api, size, again):
    data = os.urandom(size)
    started = api.post(BASE + "/initiate", json={"filename": "data.bin", "size": size, "sha256_hash": hashlib.sha256(data).hexdigest()}, headers={"Idempotency-Key": f"k{size}"})
    assert started.status_code == 200, started.text
    upload = started.json()
    part_size, count = upload["part_size"], upload["part_count"]
    assert count == -(-size // part_size)
    pieces = {number: data[(number - 1) * part_size:number * part_size] for number in range(1, count + 1)}
    assert all(pieces.values())
    signed = api.post(BASE + f"/{upload['upload_id']}/parts", json={"parts": [{"part_number": n, "crc32c": crc32c(piece)} for n, piece in pieces.items()]})
    assert signed.status_code == 200, signed.text
    for part in signed.json()["parts"]:
        assert "x-amz-checksum-crc32c" in part["headers"]
        for _ in range(2 if again and part["part_number"] == 1 else 1):
            status, body, _ = send("PUT", part["url"], pieces[part["part_number"]], part["headers"])
            assert status == 200, body
    finished = api.post(BASE + "/complete", json={"upload_id": upload["upload_id"], "sha256_hash": hashlib.sha256(data).hexdigest(), "crc32c": crc32c(data)})
    assert finished.status_code == 200, finished.text
    file = finished.json()
    assert file["size"] == size
    link = api.get(f"/files/{file['mfid']}/download_link")
    assert link.status_code == 200, link.text
    status, body, headers = send("GET", link.json()["url"])
    assert status == 200 and body == data
    assert 'filename="data.bin"' in {name.lower(): value for name, value in headers.items()}["content-disposition"]


def test_a_wrong_whole_file_checksum_is_refused(api):
    data = os.urandom(10)
    upload = api.post(BASE + "/initiate", json={"filename": "bad.bin", "size": len(data)}, headers={"Idempotency-Key": "bad"}).json()
    part = api.post(BASE + f"/{upload['upload_id']}/parts", json={"parts": [{"part_number": 1, "crc32c": crc32c(data)}]}).json()["parts"][0]
    assert send("PUT", part["url"], data, part["headers"])[0] == 200
    refused = api.post(BASE + "/complete", json={"upload_id": upload["upload_id"], "sha256_hash": hashlib.sha256(data).hexdigest(), "crc32c": crc32c(b"other")})
    assert refused.status_code == 422
    assert "object_integrity_mismatch" in refused.text


def test_an_empty_file_uploads_and_downloads(api):
    digest = hashlib.sha256(b"").hexdigest()
    started = api.post(BASE + "/initiate", json={"filename": "empty.txt", "size": 0, "sha256_hash": digest}, headers={"Idempotency-Key": "empty"})
    assert started.status_code == 200, started.text
    assert started.json()["part_count"] == 0
    finished = api.post(BASE + "/complete", json={"upload_id": started.json()["upload_id"], "sha256_hash": digest, "crc32c": crc32c(b"")})
    assert finished.status_code == 200, finished.text
    link = api.get(f"/files/{finished.json()['mfid']}/download_link")
    status, body, _ = send("GET", link.json()["url"])
    assert (status, body) == (200, b"")
