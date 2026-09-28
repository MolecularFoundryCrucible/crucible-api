import base64
import hashlib
from datetime import timedelta
from unittest.mock import patch

import google_crc32c
import pytest
from sqlmodel import Session, select

from app.api.routes import multipart as routes
from app.models.core_models import AccessGroup, AssociatedFile, Dataset, Resource, ResourceACL, UploadSession, User, UserAccessGroupLink
from app.services import multipart as service
from app.services.multipart_storage import StorageFailure, UploadMissing
from app.utils.multipart_config import MultipartStorageConfig
from app.utils.permissions import Role

BASE = "/datasets/ds/upload/multipart"
DATA = b"verified scientific data"
SHA = hashlib.sha256(DATA).hexdigest()
CRC = base64.b64encode(google_crc32c.Checksum(DATA).digest()).decode()


class MemoryStorage:
    def __init__(self):
        self.pending = {}
        self.objects = {}
        self.serial = 0
        self.completions = 0
        self.fail_after_complete = False
        self.fail_after_initiate = False
        self.read_hook = None
        self.deleted = []

    def initiate(self, upload):
        self.serial += 1
        identifier = str(self.serial)
        self.pending[identifier] = {"key": upload.gcs_path, "parts": {}}
        if self.fail_after_initiate:
            self.fail_after_initiate = False
            raise StorageFailure()
        return identifier

    def uploads(self, upload):
        return [key for key, value in self.pending.items() if value["key"] == upload.gcs_path]

    def sign_part(self, upload, number, checksum, size, expires_at):
        return f"https://storage.example/{upload.provider_upload_id}/{number}", {"x-goog-hash": f"crc32c={checksum}", "Content-Length": str(size)}

    def put(self, identifier, number, data=DATA):
        self.pending[identifier]["parts"][number] = data

    def list_parts(self, upload, marker=0, limit=1000):
        if upload.provider_upload_id not in self.pending:
            raise UploadMissing()
        parts = self.pending[upload.provider_upload_id]["parts"]
        numbers = sorted(number for number in parts if number > marker)
        page = numbers[:limit]
        return [{"part_number": n, "size": len(parts[n]), "etag": hashlib.md5(parts[n]).hexdigest()} for n in page], page[-1] if len(numbers) > limit else None

    def complete(self, upload, parts):
        pending = self.pending.pop(upload.provider_upload_id)
        data = b"".join(pending["parts"][part["part_number"]] for part in parts)
        self.objects[upload.gcs_path] = {"generation": "7", "size": len(data), "crc32c": base64.b64encode(google_crc32c.Checksum(data).digest()).decode(), "data": data}
        self.completions += 1
        if self.fail_after_complete:
            self.fail_after_complete = False
            raise StorageFailure()

    def metadata(self, upload):
        return self.objects.get(upload.gcs_path)

    def sha256(self, upload, generation, heartbeat):
        assert generation == self.objects[upload.gcs_path]["generation"]
        if self.read_hook:
            self.read_hook()
        heartbeat()
        return hashlib.sha256(self.objects[upload.gcs_path]["data"]).hexdigest()

    def abort(self, upload, identifier):
        self.pending.pop(identifier, None)

    def delete(self, upload, generation):
        assert self.objects[upload.gcs_path]["generation"] == generation
        self.deleted.append((upload.gcs_path, generation))
        self.objects.pop(upload.gcs_path)

    def download(self, bucket, key, generation, expires_at):
        assert generation == "7"
        assert key in self.objects
        return "https://download.example/pinned-generation-7"


@pytest.fixture
def flow(db_engine, client, auth_as, monkeypatch):
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
    monkeypatch.setattr(routes, "engine", db_engine)
    monkeypatch.setenv("MULTIPART_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("MULTIPART_UPLOAD_BUCKET", "dedicated")
    monkeypatch.setenv("MULTIPART_UPLOAD_SIGNER", "signer@example.iam.gserviceaccount.com")
    monkeypatch.setenv("MULTIPART_UPLOAD_PROJECT", "test-project")
    storage = MemoryStorage()
    monkeypatch.setattr(routes, "get_multipart_storage", lambda: storage)
    worker = service.MultipartWorker(db_engine, storage, MultipartStorageConfig.from_env())
    return client, db_engine, storage, worker


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


def start(flow, filename="data.bin", digest=SHA):
    client, engine, storage, worker = flow
    response = client.post(BASE + "/initiate", json={"filename": filename, "size": len(DATA), "sha256_hash": digest}, headers={"Idempotency-Key": filename})
    assert response.status_code == 202, response.text
    assert response.headers["cache-control"] == "no-store"
    identifier = response.json()["upload_id"]
    assert worker.run_once()
    assert row(engine, identifier).state == "uploading"
    return identifier


def put(flow, identifier):
    client, engine, storage, _ = flow
    response = client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]})
    assert response.status_code == 200, response.text
    assert response.json()["parts"][0]["headers"]["Content-Length"] == str(len(DATA))
    upload = row(engine, identifier)
    storage.put(upload.provider_upload_id, 1)


def complete(flow, identifier, digest=SHA, crc=CRC):
    client, _, _, _ = flow
    return client.post(BASE + "/complete", json={"upload_id": identifier, "sha256_hash": digest, "crc32c": crc})


def publish(flow):
    identifier = start(flow)
    put(flow, identifier)
    assert complete(flow, identifier).status_code == 202
    assert flow[3].run_once()
    assert flow[3].run_once()
    return identifier


def test_upload_resume_publish_retry_download_ingestion_and_delete(flow, monkeypatch):
    client, engine, storage, worker = flow
    identifier = start(flow)
    put(flow, identifier)
    progress = client.get(BASE + f"/{identifier}").json()
    assert progress["parts"][0]["size"] == len(DATA)
    assert complete(flow, identifier).status_code == 202
    assert complete(flow, identifier).status_code == 202
    assert worker.run_once()
    assert row(engine, identifier).state == "verifying"
    assert worker.run_once()
    response = complete(flow, identifier)
    assert response.status_code == 200, response.text
    file_id = response.json()["mfid"]
    assert complete(flow, identifier).json()["mfid"] == file_id
    assert storage.completions == 1
    with Session(engine) as session:
        file = session.exec(select(AssociatedFile)).one()
        assert file.sha256_verified_at is not None
        assert file.storage_generation == "7"
        assert file.sha256_hash == SHA
    import app.services.multipart_storage as adapter
    monkeypatch.setattr(adapter, "get_multipart_storage", lambda: storage)
    assert client.get(f"/files/{file_id}/download_link").json()["url"].endswith("pinned-generation-7")
    assert client.get("/datasets/ds/download_links").json()[file_id].endswith("pinned-generation-7")
    with patch("app.api.routes.files.send_pika_message") as send:
        response = client.post(f"/files/{file_id}/ingest")
        assert response.status_code == 200, response.text
        assert "dedicated/uploads/ds/" in send.call_args.kwargs["message"]
    assert client.delete(BASE + f"/{identifier}").status_code == 409
    assert client.delete(f"/files/{file_id}").status_code == 200
    assert worker.run_once()
    assert storage.objects == {}
    assert storage.deleted[0][1] == "7"


def test_idempotency_conflict_and_filename_reservation(flow):
    client, _, _, _ = flow
    identifier = start(flow)
    original = {"filename": "data.bin", "size": len(DATA), "sha256_hash": SHA}
    response = client.post(BASE + "/initiate", json=original, headers={"Idempotency-Key": "data.bin"})
    assert response.json()["upload_id"] == identifier
    assert client.post(BASE + "/initiate", json=original | {"size": 99}, headers={"Idempotency-Key": "data.bin"}).status_code == 409
    assert client.post(BASE + "/initiate", json=original).status_code == 409


def test_checksum_immutability_rate_and_part_bounds(flow):
    client, engine, _, _ = flow
    identifier = start(flow)
    put(flow, identifier)
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]}).status_code == 429
    change(engine, identifier, part_signing_after=None)
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": "AAAAAA=="}]}).status_code == 409
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 2, "crc32c": CRC}]}).status_code == 422
    assert complete(flow, identifier, digest="0" * 64).status_code == 409


def test_abort_after_access_revocation_and_cross_owner_denial(flow, auth_as):
    client, engine, storage, worker = flow
    identifier = start(flow)
    with Session(engine) as session:
        acl = session.exec(select(ResourceACL)).one()
        session.delete(acl)
        session.commit()
    assert client.get(BASE + f"/{identifier}").status_code == 403
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]}).status_code == 403
    auth_as("intruder")
    assert client.delete(BASE + f"/{identifier}").status_code == 403
    auth_as("owner")
    assert client.delete(BASE + f"/{identifier}").status_code == 202
    assert worker.run_once()
    assert not storage.pending
    assert client.delete(BASE + f"/{identifier}").status_code == 204


@pytest.mark.parametrize("phase", ["initiate", "complete"])
def test_lost_provider_response_recovers_without_duplicate(flow, phase):
    client, engine, storage, worker = flow
    if phase == "initiate":
        storage.fail_after_initiate = True
        response = client.post(BASE + "/initiate", json={"filename": "data.bin", "size": len(DATA)})
        identifier = response.json()["upload_id"]
        worker.run_once()
        change(engine, identifier, next_attempt_at=service.now())
        worker.run_once()
        assert storage.serial == 1
    else:
        identifier = start(flow)
        put(flow, identifier)
        storage.fail_after_complete = True
        complete(flow, identifier)
        worker.run_once()
        change(engine, identifier, next_attempt_at=service.now())
        worker.run_once()
        worker.run_once()
        assert row(engine, identifier).state == "completed"
        assert storage.completions == 1


@pytest.mark.parametrize("failure", ["sha256", "crc32c", "missing", "revoked"])
def test_failure_never_publishes_and_cleans(flow, failure):
    client, engine, storage, worker = flow
    identifier = start(flow, digest="0" * 64 if failure == "sha256" else SHA)
    put(flow, identifier)
    if failure == "missing":
        storage.pending[row(engine, identifier).provider_upload_id]["parts"].clear()
    assert complete(flow, identifier, digest="0" * 64 if failure == "sha256" else SHA, crc="AAAAAA==" if failure == "crc32c" else CRC).status_code == 202
    worker.run_once()
    if failure == "revoked":
        with Session(engine) as session:
            session.delete(session.exec(select(ResourceACL)).one())
            session.commit()
    worker.run_once()
    worker.run_once()
    assert row(engine, identifier).state == "failed"
    assert not storage.objects
    assert not storage.pending
    with Session(engine) as session:
        assert session.exec(select(AssociatedFile)).all() == []


def test_abort_during_verification_prevents_publication(flow):
    client, engine, storage, worker = flow
    identifier = start(flow)
    put(flow, identifier)
    complete(flow, identifier)
    worker.run_once()
    storage.read_hook = lambda: client.delete(BASE + f"/{identifier}")
    worker.run_once()
    worker.run_once()
    assert row(engine, identifier).state == "aborted"
    assert not storage.objects


def test_expiry_and_disabled_initiation_keep_cleanup_available(flow, monkeypatch):
    client, engine, storage, worker = flow
    identifier = start(flow)
    monkeypatch.setenv("MULTIPART_UPLOAD_ENABLED", "false")
    assert client.post(BASE + "/initiate", json={"filename": "other", "size": 1}).status_code == 503
    change(engine, identifier, expires_at=(service.now() - timedelta(seconds=1)).isoformat(), next_attempt_at=service.now())
    assert client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]}).status_code == 410
    worker.run_once()
    worker.run_once()
    assert row(engine, identifier).state == "expired"
    assert not storage.pending


def test_active_lease_excludes_second_worker_and_stale_writer(flow):
    _, engine, storage, worker = flow
    identifier = start(flow)
    change(engine, identifier, next_attempt_at=service.now())
    upload = worker.claim()
    second = service.MultipartWorker(engine, storage, worker.config)
    assert second.claim() is None
    change(engine, identifier, lease_expires_at=service.now() - timedelta(seconds=1))
    assert second.claim().upload_id == upload.upload_id
    with pytest.raises(service.LeaseLost):
        worker.update(identifier, state="failed")


def test_verified_dedup_and_patch_invalidates_trust(flow):
    client, _, _, _ = flow
    identifier = publish(flow)
    file_id = complete(flow, identifier).json()["mfid"]
    body = {"filename": "another", "size": len(DATA), "sha256_hash": SHA}
    response = client.post(BASE + "/initiate", json=body)
    assert response.json()["existing_file"]["mfid"] == file_id
    assert client.patch(f"/files/{file_id}", json={"sha256_hash": "0" * 64}).status_code == 200
    response = client.post(BASE + "/initiate", json=body | {"sha256_hash": "0" * 64})
    assert response.status_code == 202
    assert response.json()["existing_file"] is None


@pytest.mark.parametrize("body", [{"filename": "../x", "size": 1}, {"filename": "/x", "size": 1}, {"filename": "x", "size": True}, {"filename": "x", "size": 1, "sha256_hash": "bad"}])
def test_invalid_initiation_is_rejected(flow, body):
    assert flow[0].post(BASE + "/initiate", json=body).status_code == 422


def test_generation_cannot_be_forged_by_patching_storage_path(flow):
    client, _, _, _ = flow
    identifier = publish(flow)
    file_id = complete(flow, identifier).json()["mfid"]
    assert client.patch(f"/files/{file_id}", json={"storage_path": "dedicated/uploads/someone-else/content"}).status_code == 200
    assert client.get(f"/files/{file_id}/download_link").status_code == 403
    assert client.post(f"/files/{file_id}/ingest").status_code == 403


def test_signing_batch_limits_and_expired_session_quota(flow, monkeypatch):
    client, engine, _, worker = flow
    monkeypatch.setenv("MULTIPART_OWNER_SESSIONS", "1")
    identifier = start(flow)
    assert client.post(BASE + "/initiate", json={"filename": "another", "size": 1}).status_code == 429
    request = {"parts": [{"part_number": n, "crc32c": CRC} for n in range(1, 66)]}
    assert client.post(BASE + f"/{identifier}/parts", json=request).status_code == 422
    request = {"parts": [{"part_number": 1, "crc32c": CRC}] * 2}
    assert client.post(BASE + f"/{identifier}/parts", json=request).status_code == 422
    change(engine, identifier, expires_at=(service.now() - timedelta(seconds=1)).isoformat(), next_attempt_at=service.now())
    worker.run_once()
    assert client.post(BASE + "/initiate", json={"filename": "another", "size": 1}).status_code == 429
    worker.run_once()
    assert client.post(BASE + "/initiate", json={"filename": "another", "size": 1}).status_code == 202


def test_more_than_1000_parts_are_frozen_from_all_provider_pages(flow):
    _, engine, storage, worker = flow
    identifier = start(flow)
    count = 1001
    change(engine, identifier, state="finalizing", part_size=1, part_count=count, size=count, claimed_crc32c=base64.b64encode(google_crc32c.Checksum(b"x" * count).digest()).decode(), claimed_sha256=hashlib.sha256(b"x" * count).hexdigest(), part_manifest={"authorized": {str(n): CRC for n in range(1, count + 1)}}, next_attempt_at=service.now())
    for n in range(1, count + 1):
        storage.put(row(engine, identifier).provider_upload_id, n, b"x")
    worker.run_once()
    assert len(row(engine, identifier).part_manifest["frozen"]) == 1001
    worker.run_once()
    assert row(engine, identifier).state == "completed"


def test_dataset_deletion_during_processing_leaves_durable_cleanup(flow):
    _, engine, storage, worker = flow
    identifier = start(flow)
    put(flow, identifier)
    complete(flow, identifier)
    worker.run_once()
    from app.utils.deletion_utils import _hard_delete_dataset
    with Session(engine) as session:
        _hard_delete_dataset("ds", session, session.get(Resource, "ds"))
        session.commit()
    worker.run_once()
    assert not storage.objects
    assert row(engine, identifier).cleaned_at is not None


def test_database_failure_after_provider_completion_retries_one_publication(flow, monkeypatch):
    _, engine, storage, worker = flow
    identifier = start(flow)
    put(flow, identifier)
    complete(flow, identifier)
    worker.run_once()
    commit = Session.commit
    failed = False

    def fail_publication_once(session):
        nonlocal failed
        if not failed and any(isinstance(item, AssociatedFile) for item in session.new):
            failed = True
            raise ConnectionError("simulated transaction failure")
        return commit(session)

    monkeypatch.setattr(Session, "commit", fail_publication_once)
    worker.run_once()
    assert row(engine, identifier).state == "verifying"
    change(engine, identifier, next_attempt_at=service.now())
    worker.run_once()
    assert row(engine, identifier).state == "completed"
    assert storage.completions == 1
    with Session(engine) as session:
        assert len(session.exec(select(AssociatedFile)).all()) == 1


def test_completed_sweep_aborts_late_orphans_without_deleting_file(flow):
    _, engine, storage, worker = flow
    identifier = publish(flow)
    upload = row(engine, identifier)
    storage.initiate(upload)
    change(engine, identifier, next_attempt_at=service.now(), expires_at=(service.now() - timedelta(days=1)).isoformat())
    worker.run_once()
    assert not storage.pending
    assert storage.objects
    assert row(engine, identifier).state == "completed"


def test_configured_upload_size_limit_is_preserved(flow, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1")
    response = flow[0].post(BASE + "/initiate", json={"filename": "large", "size": 2})
    assert response.status_code == 413


def test_reference_client_resumes_after_lost_part_response(flow, tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    from scripts import multipart_upload_client as example
    client, _, storage, worker = flow
    source = tmp_path / "data.bin"
    source.write_bytes(DATA)
    resume = tmp_path / "resume.json"
    monkeypatch.setenv("CRUCIBLE_API_TOKEN", "not-persisted")
    monkeypatch.setattr("sys.argv", ["client", "--api", "https://api.example", "--dataset", "ds", "--file", str(source), "--state", str(resume)])
    monkeypatch.setattr(example.time, "sleep", lambda _: None)

    def api_request(self, method, path, **kwargs):
        if method == "GET":
            worker.run_once()
        response = client.request(method, path, **kwargs)
        assert response.status_code < 300, response.text
        if path.endswith("/complete"):
            worker.run_once()
            worker.run_once()
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
def test_real_authentication_supports_both_credentials(flow, monkeypatch, credential_kind):
    from uuid import uuid4
    from sqlmodel import SQLModel
    from app.api.main import app
    from app.models.core_models import ApiToken, HubTokenFamily, HubRefreshToken, HubAccessToken
    from app.utils import auth_utils
    from app.utils.hub_tokens import issue_hub_tokens, refresh_hub_tokens, revoke_hub_tokens

    client, engine, storage, worker = flow
    app.dependency_overrides.pop(auth_utils.authenticate_user, None)
    monkeypatch.setattr(auth_utils, "engine", engine)
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
    assert response.status_code == 202, response.text
    identifier = response.json()["upload_id"]
    worker.run_once()
    if credential_kind == "oauth":
        pair = refresh_hub_tokens(engine, pair.refresh_token, "hub")
        headers = {"Authorization": f"Bearer {pair.access_token}"}
    assert client.get(BASE + f"/{identifier}", headers=headers).status_code == 200
    signed = client.post(BASE + f"/{identifier}/parts", json={"parts": [{"part_number": 1, "crc32c": CRC}]}, headers=headers)
    assert signed.status_code == 200, signed.text
    storage.put(row(engine, identifier).provider_upload_id, 1)
    body = {"upload_id": identifier, "sha256_hash": SHA, "crc32c": CRC}
    assert client.post(BASE + "/complete", json=body, headers=headers).status_code == 202
    worker.run_once()
    worker.run_once()
    assert client.post(BASE + "/complete", json=body, headers=headers).status_code == 200
    if credential_kind == "oauth":
        revoke_hub_tokens(engine, pair.refresh_token, "hub")
        assert client.get(BASE + f"/{identifier}", headers=headers).status_code == 401
        assert client.delete(BASE + f"/{identifier}", headers=headers).status_code == 401


def test_provider_disappearance_becomes_recoverable_failure(flow):
    _, engine, storage, worker = flow
    identifier = start(flow)
    storage.pending.clear()
    change(engine, identifier, next_attempt_at=service.now())
    worker.run_once()
    assert row(engine, identifier).failure_code == "provider_upload_missing"
    assert row(engine, identifier).state == "failed"
    worker.run_once()
    assert row(engine, identifier).cleaned_at is not None


def test_worker_release_preserves_newly_queued_cancellation(flow):
    client, engine, _, worker = flow
    identifier = start(flow)
    change(engine, identifier, next_attempt_at=service.now())
    claimed = worker.claim()
    assert client.delete(BASE + f"/{identifier}").status_code == 202
    worker.release(identifier, delay=3600, expected_schedule=claimed.next_attempt_at)
    assert service.utc(row(engine, identifier).next_attempt_at) <= service.now()



def test_completion_worker_respects_normal_privilege_mode(flow, auth_as):
    from app.utils.permissions import PlatformRole
    client, engine, _, worker = flow
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == "owner")).one()
        user.platform_role = "admin"
        session.add(user)
        session.commit()
        group = session.exec(select(AccessGroup)).one()
        group_id = group.id
    auth_as("owner", groups=[("owner-group", group_id, Role.OWNER)], platform_role=PlatformRole.ADMIN)
    identifier = start(flow)
    assert row(engine, identifier).part_manifest["allow_global_admin"] is True
    put(flow, identifier)
    body = {"upload_id": identifier, "sha256_hash": SHA, "crc32c": CRC}
    response = client.post(BASE + "/complete", json=body, headers={"Crucible-Privilege-Mode": "normal"})
    assert response.status_code == 202, response.text
    assert row(engine, identifier).part_manifest["allow_global_admin"] is False
    with Session(engine) as session:
        session.delete(session.exec(select(ResourceACL)).one())
        session.commit()
    worker.run_once()
    assert row(engine, identifier).failure_code == "access_revoked"
    with Session(engine) as session:
        assert session.exec(select(AssociatedFile)).all() == []


@pytest.mark.parametrize("generation", [None, "7"])
def test_cleanup_protection_requires_server_recorded_generation(flow, generation):
    client, engine, storage, worker = flow
    identifier = start(flow)
    put(flow, identifier)
    complete(flow, identifier)
    worker.run_once()
    upload = row(engine, identifier)
    with Session(engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        session.add(AssociatedFile(dataset_id=dataset.id, filename="reference", storage_path=f"{upload.bucket}/{upload.gcs_path}", size=len(DATA), sha256_hash=SHA, storage_generation=generation))
        session.commit()
    assert client.delete(BASE + f"/{identifier}").status_code == 202
    worker.run_once()
    if generation is None:
        assert not storage.objects
        assert row(engine, identifier).cleaned_at is not None
    else:
        assert storage.objects
        assert row(engine, identifier).failure_code == "cleanup_object_still_registered"
        assert row(engine, identifier).cleaned_at is None
