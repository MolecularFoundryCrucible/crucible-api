import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import google_crc32c
import pytest
from google.api_core.exceptions import NotFound
from sqlmodel import Session, select

import app.api.routes.gcs_storage as gcs_storage
import app.storage.gcs as google
from app.api.routes import multipart as routes
from app.models.core_models import AccessGroup, AssociatedFile, Dataset, DeletionRequest, Resource, ResourceACL, UploadSession, User, UserAccessGroupLink
from app.models.datasets import file_etag
from app.services import multipart as service
from app.storage import ObjectInfo, Part, StorageFailure, UploadMissing
from app.utils.permissions import Role

BASE = "/datasets/ds/upload/multipart"
DATA = b"verified scientific data"
SHA = hashlib.sha256(DATA).hexdigest()
CRC = base64.b64encode(google_crc32c.Checksum(DATA).digest()).decode()


class MemoryStorage:
    retires_by_lifecycle = True

    def __init__(self, reports_crc32c=True):
        self.reports_crc32c = reports_crc32c
        self.reports_part_crc32c = not reports_crc32c
        self.pending = {}
        self.objects = {}
        self.serial = 0
        self.completions = 0
        self.fail_after_complete = False
        self.fail_after_initiate = False
        self.complete_hook = None
        self.deleted = []
        self.finished_checksums = []

    def start_upload(self, bucket, key):
        self.serial += 1
        identifier = str(self.serial)
        self.pending[identifier] = {"key": key, "parts": {}}
        if self.fail_after_initiate:
            self.fail_after_initiate = False
            raise StorageFailure()
        return identifier

    def open_uploads(self, bucket, key):
        return [identifier for identifier, value in self.pending.items() if value["key"] == key]

    def sign_part(self, bucket, key, upload_id, number, crc32c, size, expires_at):
        return f"https://storage.example/{upload_id}/{number}", {"x-goog-hash": f"crc32c={crc32c}", "Content-Length": str(size)}

    def put(self, identifier, number, data=DATA):
        self.pending[identifier]["parts"][number] = data

    def list_parts(self, bucket, key, upload_id, marker=0, limit=1000):
        if upload_id not in self.pending:
            raise UploadMissing()
        parts = self.pending[upload_id]["parts"]
        numbers = sorted(number for number in parts if number > marker)
        page = numbers[:limit]
        def crc(data):
            return base64.b64encode(google_crc32c.Checksum(data).digest()).decode() if self.reports_part_crc32c else None
        return [Part(part_number=n, size=len(parts[n]), etag=hashlib.md5(parts[n]).hexdigest(), crc32c=crc(parts[n])) for n in page], page[-1] if len(numbers) > limit else None

    def finish_upload(self, bucket, key, upload_id, parts, crc32c_by_part):
        pending = self.pending.pop(upload_id)
        data = b"".join(pending["parts"][part.part_number] for part in parts)
        self.objects[key] = {"generation": "7", "size": len(data), "crc32c": base64.b64encode(google_crc32c.Checksum(data).digest()).decode(), "data": data}
        self.finished_checksums.append(crc32c_by_part)
        self.completions += 1
        if self.complete_hook:
            self.complete_hook()
        if self.fail_after_complete:
            self.fail_after_complete = False
            raise StorageFailure()

    def put_bytes(self, bucket, key, data, content_type):
        self.objects[key] = {"generation": "7", "size": len(data), "crc32c": base64.b64encode(google_crc32c.Checksum(data).digest()).decode(), "data": data}

    def head(self, bucket, key):
        stored = self.objects.get(key)
        return ObjectInfo(size=stored["size"], crc32c=stored["crc32c"] if self.reports_crc32c else None, version=stored["generation"]) if stored else None

    def abort_upload(self, bucket, key, upload_id):
        self.pending.pop(upload_id, None)

    def delete(self, bucket, key, version):
        assert self.objects[key]["generation"] == version
        self.deleted.append((key, version))
        self.objects.pop(key)


@pytest.fixture(params=["store_crc32c", "combined_crc32c"])
def flow(request, db_engine, client, auth_as, monkeypatch):
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
    monkeypatch.setenv("MULTIPART_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("UPLOAD_BUCKET", "dedicated")
    monkeypatch.setenv("MULTIPART_UPLOAD_BUCKET", "dedicated")
    monkeypatch.setenv("MULTIPART_UPLOAD_SIGNER", "signer@example.iam.gserviceaccount.com")
    monkeypatch.setenv("MULTIPART_UPLOAD_PROJECT", "test-project")
    storage = MemoryStorage(reports_crc32c=request.param == "store_crc32c")
    monkeypatch.setattr(routes, "get_store", lambda: storage)
    return client, db_engine, storage


def row(engine, identifier):
    with Session(engine) as session:
        upload = session.exec(select(UploadSession).where(UploadSession.upload_id == identifier)).one()
        session.expunge(upload)
        return upload


def change(engine, identifier, **values):
    with Session(engine) as session:
        upload = session.exec(select(UploadSession).where(UploadSession.upload_id == identifier)).one()
        for key, value in values.items():
            setattr(upload, key, value)
        session.add(upload)
        session.commit()


def files(engine):
    with Session(engine) as session:
        return session.exec(select(AssociatedFile)).all()


def start(flow, filename="data.bin", digest=SHA):
    client, engine, _ = flow
    response = client.post(BASE + "/initiate", json={"filename": filename, "size": len(DATA), "sha256_hash": digest}, headers={"Idempotency-Key": filename})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["state"] == "uploading"
    return response.json()["upload_id"]


def put(flow, identifier):
    client, engine, storage = flow
    response = client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]})
    assert response.status_code == 200, response.text
    assert response.json()["parts"][0]["headers"]["Content-Length"] == str(len(DATA))
    storage.put(row(engine, identifier).provider_upload_id, 1)


def complete(flow, identifier, digest=SHA, crc=CRC):
    return flow[0].post(BASE + "/complete", json={"upload_id": identifier, "sha256_hash": digest, "crc32c": crc})


def publish(flow, filename="data.bin"):
    identifier = start(flow, filename)
    put(flow, identifier)
    response = complete(flow, identifier)
    assert response.status_code == 200, response.text
    return identifier


def expire(engine, identifier):
    change(engine, identifier, expires_at=service.now() - timedelta(seconds=1))


def test_upload_resume_publish_retry_download_ingestion_and_delete(flow, monkeypatch):
    client, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    progress = client.get(BASE + f"/{identifier}").json()
    assert progress["parts"][0]["size"] == len(DATA)
    response = complete(flow, identifier)
    assert response.status_code == 200, response.text
    file_id = response.json()["mfid"]
    assert complete(flow, identifier).json()["mfid"] == file_id
    assert storage.completions == 1
    assert client.get(BASE + f"/{identifier}").json()["existing_file"]["mfid"] == file_id
    file = files(engine)[0]
    assert file.storage_path == f"dedicated/ds/_files/{identifier}"
    assert file.sha256_verified_at is None
    assert file.storage_generation == "7"
    assert file.sha256_hash == SHA
    signer = MagicMock()
    signed_blob = signer.bucket.return_value.blob.return_value
    signed_blob.content_type = "application/octet-stream"
    signed_blob.generate_signed_url.return_value = "https://download.example/pinned-generation-7"
    monkeypatch.setattr(google, "key_client", lambda: signer)
    monkeypatch.setattr(gcs_storage, "get_store", lambda: google.GoogleStore(SimpleNamespace(bucket=None, signer_service_account=None, project=None)))
    assert client.get(f"/files/{file_id}/download_link").json()["url"].endswith("pinned-generation-7")
    assert signer.bucket.return_value.blob.call_args.kwargs["generation"] == 7
    assert "filename*=UTF-8''data.bin" in signed_blob.generate_signed_url.call_args.kwargs["response_disposition"]
    assert client.get("/datasets/ds/download_links").json()[file_id].endswith("pinned-generation-7")
    with patch("app.api.routes.files.send_pika_message") as send:
        response = client.post(f"/files/{file_id}/ingest")
        assert response.status_code == 200, response.text
        assert "dedicated/ds/_files/" in send.call_args.kwargs["message"]
        assert json.loads(send.call_args.kwargs["message"])["file_name"] == "data.bin"
    assert client.delete(BASE + f"/{identifier}").status_code == 409
    assert client.delete(f"/files/{file_id}").status_code == 200
    assert complete(flow, identifier).status_code == 410


def test_idempotency_conflict_and_filename_reservation(flow):
    client, _, storage = flow
    identifier = start(flow)
    original = {"filename": "data.bin", "size": len(DATA), "sha256_hash": SHA}
    response = client.post(BASE + "/initiate", json=original, headers={"Idempotency-Key": "data.bin"})
    assert response.json()["upload_id"] == identifier
    assert storage.serial == 1
    assert client.post(BASE + "/initiate", json=original | {"size": 99}, headers={"Idempotency-Key": "data.bin"}).status_code == 409
    assert client.post(BASE + "/initiate", json=original).status_code == 409


def test_checksum_immutability_rate_and_part_bounds(flow):
    client, engine, _ = flow
    identifier = start(flow)
    put(flow, identifier)
    response = client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "5"
    change(engine, identifier, part_signing_after=None)
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": "AAAAAA=="}]}).status_code == 409
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 2, "crc32c": CRC}]}).status_code == 422
    assert complete(flow, identifier, digest="0" * 64).status_code == 409
    assert complete(flow, identifier).status_code == 200
    assert complete(flow, identifier, crc="AAAAAA==").status_code == 409


def test_completion_requires_every_part_to_be_authorized(flow):
    identifier = start(flow)
    assert complete(flow, identifier).status_code == 409
    assert row(flow[1], identifier).state == "uploading"


def test_abort_after_access_revocation_and_cross_owner_denial(flow, auth_as):
    client, engine, storage = flow
    identifier = start(flow)
    with Session(engine) as session:
        session.delete(session.exec(select(ResourceACL)).one())
        session.commit()
    assert client.get(BASE + f"/{identifier}").status_code == 403
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]}).status_code == 403
    assert complete(flow, identifier).status_code == 403
    auth_as("intruder")
    assert client.delete(BASE + f"/{identifier}").status_code == 403
    auth_as("owner")
    assert client.delete(BASE + f"/{identifier}").status_code == 204
    assert not storage.pending
    assert row(engine, identifier).state == "aborted"
    assert row(engine, identifier).cleaned_at is not None
    assert client.delete(BASE + f"/{identifier}").status_code == 204


def test_lost_initiation_response_reuses_provider_upload(flow):
    client, engine, storage = flow
    storage.fail_after_initiate = True
    request = {"filename": "data.bin", "size": len(DATA)}
    assert client.post(BASE + "/initiate", json=request, headers={"Idempotency-Key": "k"}).status_code == 503
    response = client.post(BASE + "/initiate", json=request, headers={"Idempotency-Key": "k"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "uploading"
    assert storage.serial == 1
    assert row(engine, response.json()["upload_id"]).provider_upload_id == "1"


def test_lost_completion_response_publishes_once(flow):
    _, _, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    storage.fail_after_complete = True
    assert complete(flow, identifier).status_code == 503
    assert complete(flow, identifier).status_code == 200
    assert storage.completions == 1
    assert len(files(flow[1])) == 1


def test_stranded_initiation_expires_and_releases_the_filename(flow):
    client, engine, _ = flow
    from app.models.multipart import MultipartInitiate
    from app.utils.auth_utils import load_principal
    from app.utils.multipart_config import MultipartStorageConfig
    with Session(engine) as session:
        principal = load_principal(session, "owner")
    upload = service.reserve(engine, principal, "ds", MultipartInitiate(filename="data.bin", size=len(DATA)), "k", MultipartStorageConfig.from_env())
    assert service.utc(upload.expires_at) <= service.now() + service.INITIATION_WINDOW
    assert client.post(BASE + "/initiate", json={"filename": "data.bin", "size": len(DATA)}).status_code == 409
    expire(engine, upload.upload_id)
    assert client.post(BASE + "/initiate", json={"filename": "data.bin", "size": len(DATA)}).status_code == 200


@pytest.mark.parametrize("failure,code", [("crc32c", "object_integrity_mismatch"), ("missing", "missing_parts")])
def test_failed_verification_never_publishes_and_cleans(flow, failure, code):
    client, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    if failure == "missing":
        storage.pending[row(engine, identifier).provider_upload_id]["parts"].clear()
    response = complete(flow, identifier, crc="AAAAAA==" if failure == "crc32c" else CRC)
    assert response.status_code == 422
    assert code in response.json()["detail"]
    assert row(engine, identifier).state == "failed"
    assert row(engine, identifier).failure_code == code
    assert row(engine, identifier).cleaned_at is not None
    assert not storage.objects
    assert not storage.pending
    assert files(engine) == []
    assert client.get(BASE + f"/{identifier}").json()["state"] == "failed"


def test_abort_during_completion_prevents_publication(flow):
    client, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    storage.complete_hook = lambda: client.delete(BASE + f"/{identifier}")
    assert complete(flow, identifier).status_code == 409
    assert row(engine, identifier).state == "aborted"
    assert not storage.objects
    assert files(engine) == []


def test_expiry_and_disabled_initiation(flow, monkeypatch):
    client, engine, storage = flow
    identifier = start(flow)
    monkeypatch.setenv("MULTIPART_UPLOAD_ENABLED", "false")
    assert client.post(BASE + "/initiate", json={"filename": "other", "size": 1}).status_code == 503
    assert client.get(BASE + f"/{identifier}").status_code == 200
    expire(engine, identifier)
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]}).status_code == 410
    assert complete(flow, identifier).status_code == 410
    assert client.get(BASE + f"/{identifier}").json()["state"] == "expired"
    assert client.delete(BASE + f"/{identifier}").status_code == 204
    assert not storage.pending


def test_claimed_hashes_do_not_short_circuit_initiation(flow):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        af = AssociatedFile(filename="claimed.bin", storage_path="globus://endpoint/claimed.bin", storage_backend="globus", size=len(DATA), sha256_hash=SHA, dataset_id=dataset.id)
        session.add(af)
        session.commit()
        file_id = af.mfid
    response = client.post(BASE + "/initiate", json={"filename": "another", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200
    assert response.json()["existing_file"] is None
    assert client.patch(f"/files/{file_id}", json={"sha256_hash": "0" * 64}).status_code == 200
    response = client.post(BASE + "/initiate", json={"filename": "third", "size": len(DATA), "sha256_hash": "0" * 64})
    assert response.status_code == 200
    assert response.json()["existing_file"] is None


def test_verified_hash_short_circuits_initiation(flow):
    client, engine, _ = flow
    identifier = publish(flow)
    file_id = complete(flow, identifier).json()["mfid"]
    with Session(engine) as session:
        file = session.exec(select(AssociatedFile)).one()
        file.sha256_verified_at = service.now()
        session.add(file)
        session.commit()
    response = client.post(BASE + "/initiate", json={"filename": "data.bin", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "completed"
    assert response.json()["upload_id"] is None
    assert response.json()["existing_file"]["mfid"] == file_id


def test_same_content_under_another_name_is_a_new_upload(flow):
    client, engine, _ = flow
    identifier = publish(flow)
    complete(flow, identifier)
    with Session(engine) as session:
        file = session.exec(select(AssociatedFile)).one()
        file.sha256_verified_at = service.now()
        session.add(file)
        session.commit()
    response = client.post(BASE + "/initiate", json={"filename": "another", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["existing_file"] is None
    assert response.json()["upload_id"] is not None


@pytest.mark.parametrize("body", [{"filename": "../x", "size": 1}, {"filename": "/x", "size": 1}, {"filename": "x", "size": True}, {"filename": "x", "size": -1}, {"filename": "x", "size": 1, "sha256_hash": "bad"}])
def test_invalid_initiation_is_rejected(flow, body):
    assert flow[0].post(BASE + "/initiate", json=body).status_code == 422


def test_storage_path_cannot_be_forged(flow):
    client, engine, _ = flow
    identifier = publish(flow)
    file_id = complete(flow, identifier).json()["mfid"]
    assert client.patch(f"/files/{file_id}", json={"storage_path": "dedicated/someone-else/_files/x"}).status_code == 422
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_id)).one()
        record.storage_path = "dedicated/someone-else/_files/x"
        session.add(record)
        session.commit()
    assert client.get(f"/files/{file_id}/download_link").status_code == 409
    assert client.post(f"/files/{file_id}/ingest").status_code == 409


def test_signing_batch_limits_and_expired_session_quota(flow, monkeypatch):
    client, engine, _ = flow
    monkeypatch.setenv("MULTIPART_OWNER_SESSIONS", "1")
    identifier = start(flow)
    assert client.post(BASE + "/initiate", json={"filename": "another", "size": 1}).status_code == 429
    request = {"parts": [{"part_number": n, "crc32c": CRC} for n in range(1, 66)]}
    assert client.post(BASE + f"/{identifier}/parts", json=request).status_code == 422
    request = {"parts": [{"part_number": 1, "crc32c": CRC}] * 2}
    assert client.post(BASE + f"/{identifier}/parts", json=request).status_code == 422
    expire(engine, identifier)
    assert client.post(BASE + "/initiate", json={"filename": "another", "size": 1}).status_code == 200


def test_stored_part_content_must_match_its_authorized_checksum(flow):
    client, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    storage.pending[row(engine, identifier).provider_upload_id]["parts"][1] = b"verified scientific dat_"
    response = complete(flow, identifier)
    assert response.status_code == 422
    code = "object_integrity_mismatch" if storage.reports_crc32c else "part_checksum_mismatch"
    assert code in response.json()["detail"]
    assert files(engine) == []


def test_a_store_reporting_no_checksums_still_completes(flow):
    _, engine, storage = flow
    storage.reports_crc32c = storage.reports_part_crc32c = False
    identifier = publish(flow)
    assert row(engine, identifier).state == "completed"


def test_more_than_1000_parts_are_read_from_all_provider_pages(flow):
    _, engine, storage = flow
    identifier = start(flow)
    count = 1001
    crc = base64.b64encode(google_crc32c.Checksum(b"x" * count).digest()).decode()
    part_crc = base64.b64encode(google_crc32c.Checksum(b"x").digest()).decode()
    change(engine, identifier, part_size=1, part_count=count, size=count, claimed_sha256=None, part_manifest={"authorized": {str(n): part_crc for n in range(1, count + 1)}})
    for n in range(1, count + 1):
        storage.put(row(engine, identifier).provider_upload_id, n, b"x")
    response = complete(flow, identifier, digest=hashlib.sha256(b"x" * count).hexdigest(), crc=crc)
    assert response.status_code == 200, response.text
    assert row(engine, identifier).state == "completed"


def test_dataset_deletion_removes_the_session(flow):
    _, engine, _ = flow
    identifier = start(flow)
    put(flow, identifier)
    from app.utils.deletion_utils import _hard_delete_dataset
    with Session(engine) as session:
        _hard_delete_dataset("ds", session, session.get(Resource, "ds"))
        session.commit()
    assert complete(flow, identifier).status_code == 404


def test_database_failure_after_provider_completion_retries_one_publication(flow, monkeypatch):
    _, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    commit = Session.commit
    failed = False

    def fail_publication_once(session):
        nonlocal failed
        if not failed and any(isinstance(item, AssociatedFile) for item in session.new):
            failed = True
            raise ConnectionError("simulated transaction failure")
        return commit(session)

    monkeypatch.setattr(Session, "commit", fail_publication_once)
    with pytest.raises(ConnectionError):
        complete(flow, identifier)
    assert row(engine, identifier).state == "finalizing"
    assert complete(flow, identifier).status_code == 200
    assert storage.completions == 1
    assert len(files(engine)) == 1


def test_completion_gives_the_store_each_authorized_part_checksum(flow):
    _, _, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    assert complete(flow, identifier).status_code == 200
    assert storage.finished_checksums == [{1: CRC}]


def test_completion_aborts_duplicate_provider_uploads(flow):
    _, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    upload = row(engine, identifier)
    storage.start_upload(upload.bucket, upload.object_key)
    assert complete(flow, identifier).status_code == 200
    assert not storage.pending
    assert storage.objects


def test_configured_upload_size_limit_is_preserved(flow, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1")
    response = flow[0].post(BASE + "/initiate", json={"filename": "large", "size": 2})
    assert response.status_code == 413


def test_reference_client_resumes_after_lost_part_response(flow, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from scripts import multipart_upload_client as example
    client, _, storage = flow
    source = tmp_path / "data.bin"
    source.write_bytes(DATA)
    resume = tmp_path / "resume.json"
    monkeypatch.setenv("CRUCIBLE_API_TOKEN", "not-persisted")
    monkeypatch.setattr("sys.argv", ["client", "--api", "https://api.example", "--dataset", "ds", "--file", str(source), "--state", str(resume)])
    monkeypatch.setattr(example.time, "sleep", lambda _: None)

    def api_request(self, method, path, timeout=60, **kwargs):
        response = client.request(method, path, **kwargs)
        assert response.status_code < 300, response.text
        return response

    monkeypatch.setattr(example.Client, "request", api_request)
    puts = 0

    def put_response(url, data, headers, **kwargs):
        nonlocal puts
        puts += 1
        assert "Authorization" not in headers
        provider, number = url.rsplit("/", 2)[1:]
        storage.put(provider, int(number), data)
        if puts == 1:
            raise RuntimeError("simulated client interruption after provider accepted part")
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(example.requests, "put", put_response)
    with pytest.raises(RuntimeError, match="interruption"):
        example.main()
    assert "not-persisted" not in resume.read_text()
    assert "storage.example" not in resume.read_text()
    example.main()
    assert puts == 1
    assert json.loads(capsys.readouterr().out)["sha256_hash"] == SHA


@pytest.mark.parametrize("credential_kind", ["api_key", "oauth"])
def test_real_authentication_supports_both_credentials(use_engine, flow, monkeypatch, credential_kind):
    from uuid import uuid4
    from sqlmodel import SQLModel
    from app.models.core_models import ApiToken, HubTokenFamily, HubRefreshToken, HubAccessToken
    from app.utils import auth_utils
    from app.utils.hub_tokens import issue_hub_tokens, refresh_hub_tokens, revoke_hub_tokens
    from app.api.main import app

    client, engine, storage = flow
    app.dependency_overrides.pop(auth_utils.authenticate_user, None)
    use_engine(engine)
    if credential_kind == "api_key":
        credential = uuid4().hex
        with Session(engine) as session:
            session.add(ApiToken(token=credential, user_unique_id="owner", access_group_id="owner-group"))
            session.commit()
    else:
        SQLModel.metadata.create_all(engine, tables=[HubTokenFamily.__table__, HubRefreshToken.__table__, HubAccessToken.__table__])
        pair = issue_hub_tokens(engine, "owner", "hub")
        credential = pair.access_token
    headers = {"Authorization": f"Bearer {credential}"}
    response = client.post(BASE + "/initiate", json={"filename": "auth.bin", "size": len(DATA), "sha256_hash": SHA}, headers=headers)
    assert response.status_code == 200, response.text
    identifier = response.json()["upload_id"]
    if credential_kind == "oauth":
        pair = refresh_hub_tokens(engine, pair.refresh_token, "hub")
        headers = {"Authorization": f"Bearer {pair.access_token}"}
    assert client.get(BASE + f"/{identifier}", headers=headers).status_code == 200
    signed = client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]}, headers=headers)
    assert signed.status_code == 200, signed.text
    storage.put(row(engine, identifier).provider_upload_id, 1)
    body = {"upload_id": identifier, "sha256_hash": SHA, "crc32c": CRC}
    assert client.post(BASE + "/complete", json=body, headers=headers).status_code == 200
    if credential_kind == "oauth":
        revoke_hub_tokens(engine, pair.refresh_token, "hub")
        assert client.get(BASE + f"/{identifier}", headers=headers).status_code == 401
        assert client.delete(BASE + f"/{identifier}", headers=headers).status_code == 401


def test_provider_disappearance_fails_the_session(flow):
    client, engine, storage = flow
    identifier = start(flow)
    storage.pending.clear()
    assert client.get(BASE + f"/{identifier}").status_code == 409
    response = complete(flow, identifier)
    assert response.status_code == 409
    put_request = client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]})
    assert put_request.status_code == 200
    response = complete(flow, identifier)
    assert response.status_code == 422
    assert row(engine, identifier).failure_code == "provider_upload_missing"
    assert row(engine, identifier).state == "failed"


def test_completion_respects_normal_privilege_mode(flow, auth_as):
    from app.utils.permissions import PlatformRole
    client, engine, _ = flow
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == "owner")).one()
        user.platform_role = "admin"
        session.add(user)
        session.commit()
        group_id = session.exec(select(AccessGroup)).one().id
    auth_as("owner", groups=[("owner-group", group_id, Role.OWNER)], platform_role=PlatformRole.ADMIN)
    identifier = start(flow)
    put(flow, identifier)
    with Session(engine) as session:
        session.delete(session.exec(select(ResourceACL)).one())
        session.commit()
    body = {"upload_id": identifier, "sha256_hash": SHA, "crc32c": CRC}
    assert client.post(BASE + "/complete", json=body, headers={"Crucible-Privilege-Mode": "normal"}).status_code == 403
    assert files(engine) == []
    assert client.post(BASE + "/complete", json=body).status_code == 200


@pytest.mark.parametrize("generation", [None, "7"])
def test_cleanup_protection_requires_server_recorded_generation(flow, generation):
    client, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    storage.fail_after_complete = True
    assert complete(flow, identifier).status_code == 503
    upload = row(engine, identifier)
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        session.add(AssociatedFile(dataset_id=dataset.id, filename="reference", storage_path=f"{upload.bucket}/{upload.object_key}", size=len(DATA), sha256_hash=SHA, storage_generation=generation))
        session.commit()
    assert client.delete(BASE + f"/{identifier}").status_code == 204
    assert bool(storage.objects) == (generation is not None)


def test_unverified_multipart_hash_does_not_dedupe_legacy_paths(use_engine, flow, monkeypatch):
    client, engine, _ = flow
    publish(flow)
    publish(flow, filename="copy.bin")
    response = client.post("/datasets/ds/files", json={"filename": "legacy.bin", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == "legacy.bin"
    use_engine(engine)
    monkeypatch.setattr("app.api.routes.gcs_storage._mint_upload_token", lambda dsid: ("token", "2030-01-01T00:00:00+00:00"))
    response = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "data.bin", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["existing_file"] is None
    response = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "legacy.bin", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["existing_file"]["filename"] == "legacy.bin"
    response = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "other.bin", "size": len(DATA), "sha256_hash": SHA})
    assert response.json()["existing_file"] is None


def test_dataset_hard_deleted_during_completion_returns_409_and_removes_the_object(flow):
    client, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)

    def delete_dataset():
        from app.utils.deletion_utils import hard_delete_resource
        with Session(engine) as session:
            hard_delete_resource("ds", session)
            session.commit()
    storage.complete_hook = delete_dataset
    response = complete(flow, identifier)
    assert response.status_code == 409, response.text
    assert "dataset_deleted" in response.text
    assert storage.objects == {}
    with Session(engine) as session:
        assert session.exec(select(AssociatedFile)).all() == []


def test_upload_does_not_publish_into_a_dataset_put_up_for_deletion(flow):
    client, engine, _ = flow
    identifier = start(flow)
    put(flow, identifier)
    with Session(engine) as session:
        session.add(DeletionRequest(resource_id="ds", resource_type="dataset", requester_id="owner", status="pending"))
        session.commit()
    response = complete(flow, identifier)
    assert response.status_code == 409, response.text
    assert "dataset_pending_deletion" in response.text
    assert row(engine, identifier).state != "completed"
    with Session(engine) as session:
        assert session.exec(select(AssociatedFile)).all() == []


def test_file_record_filenames_are_unique_within_a_dataset(flow):
    client, _, _ = flow
    file_id = complete(flow, publish(flow)).json()["mfid"]
    repeated = client.post("/datasets/ds/files", json={"filename": "data.bin", "size": len(DATA), "sha256_hash": SHA})
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["mfid"] == file_id
    changed = client.post("/datasets/ds/files", json={"filename": "data.bin", "size": len(DATA), "sha256_hash": "2" * 64})
    assert changed.status_code == 409
    detail = changed.json()["detail"]
    assert (detail["code"], detail["file_mfid"]) == ("filename_exists", file_id)
    assert detail["etag"] == client.get(f"/files/{file_id}").json()["etag"]
    renamed = client.post("/datasets/ds/files", json={"filename": "alias.bin", "size": len(DATA), "sha256_hash": SHA})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["filename"] == "alias.bin"
    assert renamed.json()["mfid"] != file_id
    assert sorted(f["filename"] for f in client.get("/datasets/ds/files").json()) == ["alias.bin", "data.bin"]


def test_legacy_paths_respect_multipart_filename_reservation(use_engine, flow, monkeypatch):
    client, engine, _ = flow
    start(flow)
    response = client.post("/datasets/ds/files", json={"filename": "data.bin", "size": len(DATA), "sha256_hash": "1" * 64})
    assert response.status_code == 409
    use_engine(engine)
    monkeypatch.setattr("app.api.routes.gcs_storage._mint_upload_token", lambda: ("token", "2030-01-01T00:00:00+00:00"))
    response = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "data.bin", "size": len(DATA)})
    assert response.status_code == 409


def test_existing_filename_conflict_is_structured(flow):
    client, engine, _ = flow
    file_id = complete(flow, publish(flow)).json()["mfid"]
    response = client.post(BASE + "/initiate", json={"filename": "data.bin", "size": len(DATA)})
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "filename_exists"
    assert detail["file_mfid"] == file_id
    assert detail["message"] == "Filename already exists"
    assert detail["etag"] == client.get(f"/files/{file_id}").json()["etag"]


REPLACE = "/files/{}/upload/multipart/initiate"


def replace_start(flow, file_id, headers=None, size=None, digest=None):
    body = {"size": size or len(DATA)}
    if digest:
        body["sha256_hash"] = digest
    return flow[0].post(REPLACE.format(file_id), json=body, headers=headers or {})


def published_file(flow, filename="data.bin"):
    return complete(flow, publish(flow, filename)).json()


def test_replace_initiation_creates_a_session_for_the_file(flow):
    client, engine, _ = flow
    file = published_file(flow)
    response = replace_start(flow, file["mfid"], headers={"If-Match": file["etag"]})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "uploading"
    session = row(engine, response.json()["upload_id"])
    assert session.replace_file_mfid == file["mfid"]
    assert session.replace_base_etag == file["etag"]
    assert session.associated_file_name == "data.bin"
    assert session.object_key == f"ds/_files/{session.upload_id}"


def test_replace_accepts_a_weakened_if_match(flow):
    file = published_file(flow)
    response = replace_start(flow, file["mfid"], headers={"If-Match": f'"other", W/{file["etag"]}'})
    assert response.status_code == 200, response.text


def test_replace_if_match_mismatch_is_412(flow):
    file = published_file(flow)
    response = replace_start(flow, file["mfid"], headers={"If-Match": '"stale"'})
    assert response.status_code == 412
    assert response.json()["detail"] == {"code": "etag_mismatch", "etag": file["etag"], "message": "File has changed since the given ETag"}


def test_second_live_replace_is_409(flow):
    file = published_file(flow)
    first = replace_start(flow, file["mfid"]).json()["upload_id"]
    response = replace_start(flow, file["mfid"])
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "replace_in_progress", "upload_id": first, "message": "Another replace of this file is in progress"}


def test_expired_replace_does_not_block_a_new_one(flow):
    client, engine, _ = flow
    file = published_file(flow)
    first = replace_start(flow, file["mfid"]).json()["upload_id"]
    expire(engine, first)
    assert replace_start(flow, file["mfid"]).status_code == 200


def test_replace_initiation_is_idempotent(flow):
    file = published_file(flow)
    headers = {"Idempotency-Key": "replace-1"}
    first = replace_start(flow, file["mfid"], headers=headers).json()["upload_id"]
    again = replace_start(flow, file["mfid"], headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["upload_id"] == first


def test_replace_of_catalog_record_is_400(flow):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        record = AssociatedFile(dataset_id=dataset.id, filename="remote.h5", size=5, sha256_hash="b" * 64, storage_backend="globus", storage_path="globus://x/remote.h5")
        session.add(record)
        session.commit()
        session.refresh(record)
        mfid = record.mfid
    response = replace_start(flow, mfid)
    assert response.status_code == 400
    assert response.json()["detail"] == "Only GCS files can be replaced; edit catalog-only records with PATCH /files/{file_id}"


def test_replace_requires_edit(flow):
    client, engine, _ = flow
    file = published_file(flow)
    with Session(engine) as session:
        acl = session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds")).one()
        acl.permission = "viewer"
        acl.write = False
        session.add(acl)
        session.commit()
    assert replace_start(flow, file["mfid"]).status_code == 403


def test_replace_with_trusted_identical_content_returns_the_file(flow):
    client, engine, _ = flow
    file = published_file(flow)
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file["mfid"])).one()
        record.sha256_verified_at = service.now()
        session.add(record)
        session.commit()
    response = replace_start(flow, file["mfid"], digest=SHA)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "completed"
    assert response.json()["existing_file"]["mfid"] == file["mfid"]
    assert response.json()["upload_id"] is None


def test_replace_denied_without_access_never_runs_the_backfill(flow, monkeypatch):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        record = AssociatedFile(dataset_id=dataset.id, filename="legacy.bin", size=len(DATA), sha256_hash=SHA, storage_backend="gcs", storage_path=None)
        session.add(record)
        session.commit()
        session.refresh(record)
        mfid = record.mfid
        acl = session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds")).one()
        acl.permission = "viewer"
        acl.write = False
        session.add(acl)
        session.commit()
    calls = []
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", lambda *args: calls.append(args) or "mf-storage-prod/ds/legacy.bin")
    response = replace_start(flow, mfid)
    assert response.status_code == 403
    assert calls == []


def test_replace_backfills_the_storage_path_before_creating_the_session(flow, monkeypatch):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        record = AssociatedFile(dataset_id=dataset.id, filename="legacy.bin", size=len(DATA), sha256_hash=SHA, storage_backend="gcs", storage_path=None)
        session.add(record)
        session.commit()
        session.refresh(record)
        mfid = record.mfid
    before_etag = client.get(f"/files/{mfid}").json()["etag"]
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", lambda *args: "mf-storage-prod/ds/legacy.bin")
    response = replace_start(flow, mfid, headers={"If-Match": before_etag})
    assert response.status_code == 200, response.text
    session = row(engine, response.json()["upload_id"])
    assert session.replace_base_etag == file_etag("mf-storage-prod/ds/legacy.bin", None, SHA, len(DATA))


def test_replace_trusted_short_circuit_persists_the_backfilled_path(flow, monkeypatch):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        record = AssociatedFile(dataset_id=dataset.id, filename="legacy.bin", size=len(DATA), sha256_hash=SHA, storage_backend="gcs", storage_path=None)
        session.add(record)
        session.commit()
        session.refresh(record)
        mfid = record.mfid
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", lambda *args: "mf-storage-prod/ds/legacy.bin")
    response = replace_start(flow, mfid, digest=SHA)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "completed"
    with Session(engine) as session:
        stored = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one()
        assert stored.storage_path == "mf-storage-prod/ds/legacy.bin"
    assert client.get(f"/files/{mfid}").json()["etag"] == response.json()["existing_file"]["etag"]


@pytest.fixture
def retired(monkeypatch):
    calls = []
    monkeypatch.setattr(routes, "retire_object", lambda store, path, generation, retired_at: calls.append((path, generation)) or True)
    return calls


def replace(flow, file_id, headers=None):
    response = replace_start(flow, file_id, headers=headers)
    assert response.status_code == 200, response.text
    identifier = response.json()["upload_id"]
    put(flow, identifier)
    return identifier, complete(flow, identifier)


def test_replace_repoints_the_same_record_and_retires_the_old_object(flow, retired):
    client, engine, _ = flow
    file = published_file(flow)
    identifier, response = replace(flow, file["mfid"], headers={"If-Match": file["etag"]})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mfid"] == file["mfid"]
    assert body["filename"] == "data.bin"
    assert body["storage_path"] == f"dedicated/ds/_files/{identifier}"
    assert body["etag"] != file["etag"]
    assert retired == [(file["storage_path"], "7")]
    assert len(files(engine)) == 1


def test_replace_moves_a_legacy_file_into_the_multipart_layout(flow, retired):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        legacy = AssociatedFile(dataset_id=dataset.id, filename="old.bin", size=3, sha256_hash="c" * 64, storage_path="dedicated/ds/old.bin")
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        mfid = legacy.mfid
    identifier, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one()
        assert record.storage_path == f"dedicated/ds/_files/{identifier}"
        assert record.storage_generation == "7"
        assert record.filename == "old.bin"
    assert retired == [("dedicated/ds/old.bin", None)]


def test_shared_previous_object_is_not_retired(flow, retired):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        first = AssociatedFile(dataset_id=dataset.id, filename="dup.bin", size=3, sha256_hash="d" * 64, storage_path="dedicated/ds/dup.bin")
        twin = AssociatedFile(dataset_id=dataset.id, filename="dup.bin", size=3, sha256_hash="d" * 64, storage_path="dedicated/ds/dup.bin")
        session.add_all([first, twin])
        session.commit()
        session.refresh(first)
        mfid = first.mfid
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == []


def test_replace_of_pathless_legacy_file_backfills_and_skips_missing_object(flow, retired, monkeypatch):
    client, engine, _ = flow
    import app.api.routes.gcs_storage as gcs_storage
    from fastapi import HTTPException
    def missing(dsid, filename):
        raise HTTPException(status_code=404, detail="File not found in production storage")
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", missing)
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        legacy = AssociatedFile(dataset_id=dataset.id, filename="nopath.bin", size=3, sha256_hash="e" * 64, storage_path=None)
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        mfid = legacy.mfid
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == []


def test_file_changed_during_replace_fails_publication(flow, retired):
    client, engine, storage = flow
    file = published_file(flow)
    response = replace_start(flow, file["mfid"])
    identifier = response.json()["upload_id"]
    put(flow, identifier)
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file["mfid"])).one()
        record.size = record.size + 1
        session.add(record)
        session.commit()
    result = complete(flow, identifier)
    assert result.status_code == 409
    assert result.json()["detail"] == "Upload could not be published: file_changed"
    assert row(engine, identifier).state == "failed"
    assert f"ds/_files/{identifier}" not in storage.objects
    assert retired == []


def test_aborted_replace_leaves_file_untouched(flow, retired):
    client, engine, _ = flow
    file = published_file(flow)
    identifier = replace_start(flow, file["mfid"]).json()["upload_id"]
    assert client.delete(BASE + f"/{identifier}").status_code == 204
    after = client.get(f"/files/{file['mfid']}").json()
    assert after["etag"] == file["etag"]
    assert retired == []


def test_retire_failure_does_not_fail_the_replace(flow, monkeypatch):
    client, engine, _ = flow
    def broken(store, path, generation, retired_at):
        raise RuntimeError("storage down")
    monkeypatch.setattr(routes, "retire_object", broken)
    file = published_file(flow)
    _, response = replace(flow, file["mfid"])
    assert response.status_code == 200, response.text


def test_retire_skips_buckets_outside_the_environment(flow, retired, monkeypatch):
    client, engine, _ = flow
    monkeypatch.setenv("STORAGE_READ_BUCKETS", "mf-storage-prod")
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        legacy = AssociatedFile(dataset_id=dataset.id, filename="old.bin", size=3, sha256_hash="c" * 64, storage_path="mf-storage-prod/ds/old.bin")
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        mfid = legacy.mfid
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == []


def test_retire_skips_an_invalid_storage_location(flow, retired):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        legacy = AssociatedFile(dataset_id=dataset.id, filename="old.bin", size=3, sha256_hash="c" * 64, storage_path="dedicated/other-ds/old.bin")
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        mfid = legacy.mfid
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == []


def test_legacy_retire_requires_exact_dataset_relative_key(flow, retired):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        legacy = AssociatedFile(dataset_id=dataset.id, filename="report.bin", size=3, sha256_hash="c" * 64, storage_path="dedicated/ds/raw/report.bin")
        session.add(legacy)
        session.commit()
        session.refresh(legacy)
        mfid = legacy.mfid
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == []


def test_legacy_retire_skips_when_a_pathless_sibling_shares_the_filename(flow, retired):
    client, engine, _ = flow
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        legacy = AssociatedFile(dataset_id=dataset.id, filename="dup.bin", size=3, sha256_hash="c" * 64, storage_path="dedicated/ds/dup.bin")
        sibling = AssociatedFile(dataset_id=dataset.id, filename="dup.bin", size=3, sha256_hash="d" * 64, storage_path=None)
        session.add_all([legacy, sibling])
        session.commit()
        session.refresh(legacy)
        mfid = legacy.mfid
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == []


def test_edit_revoked_during_replace_completion_fails_with_403(flow, retired):
    client, engine, storage = flow
    file = published_file(flow)
    response = replace_start(flow, file["mfid"])
    identifier = response.json()["upload_id"]
    put(flow, identifier)
    with Session(engine) as session:
        acl = session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds")).one()
        acl.permission = "contributor"
        session.add(acl)
        session.commit()
    result = complete(flow, identifier)
    assert result.status_code == 403
    assert result.json()["detail"] == "Upload could not be published: permission_revoked"
    assert row(engine, identifier).state == "failed"
    assert f"ds/_files/{identifier}" not in storage.objects
    assert retired == []


def test_storage_backend_changed_during_replace_fails_publication(flow, retired):
    client, engine, storage = flow
    file = published_file(flow)
    response = replace_start(flow, file["mfid"])
    identifier = response.json()["upload_id"]
    put(flow, identifier)
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file["mfid"])).one()
        assert file_etag(record.storage_path, record.storage_generation, record.sha256_hash, record.size) == file["etag"]
        record.storage_backend = "local"
        session.add(record)
        session.commit()
    result = complete(flow, identifier)
    assert result.status_code == 409
    assert result.json()["detail"] == "Upload could not be published: file_changed"
    assert row(engine, identifier).state == "failed"
    assert f"ds/_files/{identifier}" not in storage.objects
    assert retired == []


def test_repeated_replace_completion_retires_once(flow, retired):
    client, engine, _ = flow
    file = published_file(flow)
    identifier, first = replace(flow, file["mfid"])
    assert first.status_code == 200, first.text
    second = complete(flow, identifier)
    assert second.status_code == 200, second.text
    assert second.json()["mfid"] == first.json()["mfid"]
    assert len(retired) == 1


def pathless_file(engine, filename="legacy.bin"):
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        record = AssociatedFile(dataset_id=dataset.id, filename=filename, size=len(DATA), sha256_hash=SHA, storage_backend="gcs", storage_path=None)
        session.add(record)
        session.commit()
        session.refresh(record)
        return record.mfid


def test_replace_idempotent_replay_precedes_if_match(flow, monkeypatch):
    client, engine, _ = flow
    mfid = pathless_file(engine)
    before_etag = client.get(f"/files/{mfid}").json()["etag"]
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", lambda *args: "dedicated/ds/legacy.bin")
    headers = {"Idempotency-Key": "replay-1", "If-Match": before_etag}
    first = replace_start(flow, mfid, headers=headers)
    assert first.status_code == 200, first.text
    again = replace_start(flow, mfid, headers=headers)
    assert again.status_code == 200, again.text
    assert again.json()["upload_id"] == first.json()["upload_id"]


def test_if_match_star_matches_any_etag(flow):
    file = published_file(flow)
    response = replace_start(flow, file["mfid"], headers={"If-Match": "*"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "uploading"


def test_replace_of_file_whose_dataset_vanished_is_404(flow):
    client, engine, _ = flow
    with Session(engine) as session:
        record = AssociatedFile(dataset_id=9999, filename="orphan.bin", size=len(DATA), sha256_hash=SHA, storage_backend="gcs", storage_path="dedicated/gone/orphan.bin")
        session.add(record)
        session.commit()
        session.refresh(record)
        mfid = record.mfid
    response = replace_start(flow, mfid)
    assert response.status_code == 404
    assert response.json()["detail"] == "File not found"


def test_identical_content_does_not_short_circuit_when_bytes_are_missing(flow, monkeypatch):
    client, engine, _ = flow
    from fastapi import HTTPException
    def missing(dsid, filename):
        raise HTTPException(status_code=404, detail="File not found in production storage")
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", missing)
    mfid = pathless_file(engine)
    response = replace_start(flow, mfid, digest=SHA)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "uploading"
    assert row(engine, response.json()["upload_id"]).replace_file_mfid == mfid


def test_replace_without_previous_path_is_logged(flow, retired, monkeypatch, caplog):
    client, engine, _ = flow
    from fastapi import HTTPException
    def missing(dsid, filename):
        raise HTTPException(status_code=404, detail="File not found in production storage")
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", missing)
    mfid = pathless_file(engine)
    with caplog.at_level("INFO", logger="app.services.multipart"):
        _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert f"file_replaced file={mfid} dataset=ds previous_path=None previous_generation=None retired=False" in caplog.messages


def legacy_file(engine, filename, siblings=()):
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        legacy = AssociatedFile(dataset_id=dataset.id, filename=filename, size=3, sha256_hash="c" * 64, storage_path=f"dedicated/ds/{filename}")
        session.add(legacy)
        session.add_all([AssociatedFile(dataset_id=dataset.id, filename=name, size=3, sha256_hash="d" * 64, storage_path=None) for name in siblings])
        session.commit()
        session.refresh(legacy)
        return legacy.mfid


@pytest.mark.parametrize("sibling", ["sub/dup.bin", "up.bin"])
def test_legacy_retire_skips_when_a_pathless_sibling_could_backfill_to_the_key(flow, retired, sibling):
    client, engine, _ = flow
    mfid = legacy_file(engine, "dup.bin", siblings=[sibling])
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == []


@pytest.mark.parametrize("expires_in", [timedelta(hours=1), None])
def test_pending_legacy_upload_blocks_retirement(flow, retired, expires_in):
    client, engine, _ = flow
    mfid = legacy_file(engine, "old.bin")
    with Session(engine) as session:
        session.add(UploadSession(dataset_mfid="ds", object_key="ds/old.bin", associated_file_name="old.bin", owner_user_id="owner", expires_at=service.now() + (expires_in or -timedelta(hours=1)), size=3))
        session.commit()
    _, response = replace(flow, mfid)
    assert response.status_code == 200, response.text
    assert retired == ([] if expires_in else [("dedicated/ds/old.bin", None)])


def test_retirement_errors_never_fail_a_committed_replace(flow, retired, monkeypatch):
    file = published_file(flow)
    def broken():
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(service, "owned_object", broken)
    _, response = replace(flow, file["mfid"])
    assert response.status_code == 200, response.text
    assert retired == []


def test_replace_routes_document_their_error_responses():
    from app.api.main import app
    paths = app.openapi()["paths"]
    replace_responses = paths["/files/{file_mfid}/upload/multipart/initiate"]["post"]["responses"]
    assert {"400", "409", "412"} <= set(replace_responses)
    assert "replace_in_progress" in replace_responses["409"]["description"]
    assert "etag_mismatch" in replace_responses["412"]["description"]
    assert "filename_exists" in paths["/datasets/{dataset_mfid}/upload/multipart/initiate"]["post"]["responses"]["409"]["description"]
    assert "ETag" in paths["/files/{file_id}"]["get"]["responses"]["200"]["headers"]


def test_backfill_listing_is_bounded(monkeypatch):
    client = MagicMock()
    blob = MagicMock()
    blob.name = "ds/legacy.bin"
    client.bucket.return_value.list_blobs.return_value = [blob]
    monkeypatch.setattr(gcs_storage, "key_client", lambda: client)
    assert gcs_storage._determine_storage_path("ds", "legacy.bin").endswith("/ds/legacy.bin")
    client.bucket.return_value.list_blobs.assert_called_once_with(prefix="ds/", timeout=30)


MIB = 1024 ** 2


@pytest.mark.parametrize("size, expected", [
    (1, 8 * MIB),
    (64 * MIB, 8 * MIB),
    (64 * MIB + 1, 9 * MIB),
    (75_000_000, 9 * MIB),
    (300 * MIB, 38 * MIB),
    (512 * MIB, 64 * MIB),
    (500 * 1024 ** 3, 64 * MIB),
])
def test_part_size_targets_eight_parallel_parts(size, expected):
    assert service.choose_part_size(size) == expected


def test_initiate_returns_adaptive_part_size(flow):
    client, _, _ = flow
    size = 75_000_000
    response = client.post(BASE + "/initiate", json={"filename": "medium.bin", "size": size})
    assert response.status_code == 200, response.text
    assert response.json()["part_size"] == 9 * MIB
    assert response.json()["part_count"] == 8


OLD = datetime(2020, 1, 1, tzinfo=timezone.utc)


def dataset_touched(engine):
    with Session(engine) as session:
        value = session.get(Resource, "ds").modification_time
        return value.replace(tzinfo=value.tzinfo or timezone.utc) > OLD


def age_dataset(engine):
    with Session(engine) as session:
        resource = session.get(Resource, "ds")
        resource.modification_time = OLD
        session.add(resource)
        session.commit()


def test_new_files_touch_the_dataset(flow):
    _, engine, _ = flow
    age_dataset(engine)
    publish(flow)
    assert dataset_touched(engine)


def test_legacy_completion_touches_the_dataset(use_engine, flow, monkeypatch):
    client, engine, _ = flow
    use_engine(engine)
    monkeypatch.setattr("app.api.routes.gcs_storage._mint_upload_token", lambda dsid: ("token", "2030-01-01T00:00:00+00:00"))
    upload_id = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "legacy.bin", "size": len(DATA)}).json()["upload_id"]
    age_dataset(engine)
    blob = SimpleNamespace(size=len(DATA), reload=lambda: None)
    gcs = MagicMock()
    gcs.bucket.return_value.blob.return_value = blob
    monkeypatch.setattr("app.api.routes.gcs_storage.key_client", lambda: gcs)
    response = client.post("/datasets/ds/upload/complete", json={"upload_id": upload_id, "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert dataset_touched(engine)


LEAKY = "https://storage.googleapis.com/mf-storage-prod/x signer@mf-crucible.iam.gserviceaccount.com"


@pytest.mark.parametrize("failure, status", [(NotFound("gone"), 422), (RuntimeError(LEAKY), 503)])
def test_legacy_completion_storage_errors_stay_server_side(use_engine, flow, monkeypatch, failure, status):
    client, engine, _ = flow
    use_engine(engine)
    monkeypatch.setattr("app.api.routes.gcs_storage._mint_upload_token", lambda dsid: ("token", "2030-01-01T00:00:00+00:00"))
    upload_id = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "legacy.bin", "size": len(DATA)}).json()["upload_id"]

    def reload():
        raise failure
    gcs = MagicMock()
    gcs.bucket.return_value.blob.return_value = SimpleNamespace(size=len(DATA), reload=reload)
    monkeypatch.setattr("app.api.routes.gcs_storage.key_client", lambda: gcs)
    response = client.post("/datasets/ds/upload/complete", json={"upload_id": upload_id, "sha256_hash": SHA})
    assert response.status_code == status
    assert "googleapis" not in response.text and "iam" not in response.text and "gone" not in response.text


def test_legacy_initiate_token_errors_stay_server_side(use_engine, flow, monkeypatch):
    client, engine, _ = flow
    use_engine(engine)

    def mint(dsid):
        raise RuntimeError(LEAKY)
    monkeypatch.setattr("app.api.routes.gcs_storage._mint_upload_token", mint)
    response = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "legacy.bin", "size": len(DATA)})
    assert response.status_code == 503
    assert "googleapis" not in response.text and "iam" not in response.text


EMPTY_SHA = hashlib.sha256(b"").hexdigest()
EMPTY_CRC = "AAAAAA=="


def start_empty(flow, filename="empty.txt"):
    response = flow[0].post(BASE + "/initiate", json={"filename": filename, "size": 0, "sha256_hash": EMPTY_SHA}, headers={"Idempotency-Key": filename})
    assert response.status_code == 200, response.text
    return response.json()


def test_an_empty_file_uploads_without_parts(flow):
    client, engine, storage = flow
    started = start_empty(flow)
    assert (started["state"], started["part_size"], started["part_count"]) == ("uploading", 0, 0)
    assert storage.pending == {}
    status = client.get(BASE + f"/{started['upload_id']}").json()
    assert (status["parts"], status["part_count"]) == ([], 0)
    assert client.post(BASE + f"/{started['upload_id']}/parts", json={"parts": [{"part_number": 1, "crc32c": EMPTY_CRC}]}).status_code == 422
    response = complete(flow, started["upload_id"], digest=EMPTY_SHA, crc=EMPTY_CRC)
    assert response.status_code == 200, response.text
    assert (response.json()["size"], response.json()["filename"]) == (0, "empty.txt")
    key = row(engine, started["upload_id"]).object_key
    assert storage.objects[key]["data"] == b""
    assert complete(flow, started["upload_id"], digest=EMPTY_SHA, crc=EMPTY_CRC).json()["mfid"] == response.json()["mfid"]


@pytest.mark.parametrize("digest, crc", [(SHA, EMPTY_CRC), (EMPTY_SHA, CRC)])
def test_an_empty_file_with_wrong_checksums_is_refused_before_storing(flow, digest, crc):
    _, engine, storage = flow
    started = start_empty(flow)
    response = complete(flow, started["upload_id"], digest=digest, crc=crc)
    assert response.status_code in (409, 422), response.text
    assert row(engine, started["upload_id"]).object_key not in storage.objects
    assert files(engine) == []


def test_a_file_can_be_replaced_with_empty_content(flow):
    client, engine, storage = flow
    file = published_file(flow)
    response = client.post(REPLACE.format(file["mfid"]), json={"size": 0, "sha256_hash": EMPTY_SHA})
    assert response.status_code == 200, response.text
    assert response.json()["part_count"] == 0
    replaced = complete(flow, response.json()["upload_id"], digest=EMPTY_SHA, crc=EMPTY_CRC)
    assert replaced.status_code == 200, replaced.text
    assert (replaced.json()["mfid"], replaced.json()["size"]) == (file["mfid"], 0)


def test_an_empty_upload_can_be_aborted(flow):
    client, engine, _ = flow
    started = start_empty(flow)
    assert client.delete(BASE + f"/{started['upload_id']}").status_code == 204
    assert row(engine, started["upload_id"]).state == "aborted"
