import base64
import hashlib
import json
from datetime import timedelta
from unittest.mock import MagicMock, patch

import google_crc32c
import pytest
from sqlmodel import Session, select

import app.api.routes.gcs_storage as gcs_storage
from app.api.routes import multipart as routes
from app.models.core_models import AccessGroup, AssociatedFile, Dataset, Resource, ResourceACL, UploadSession, User, UserAccessGroupLink
from app.services import multipart as service
from app.services.multipart_storage import StorageFailure, UploadMissing
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
        self.complete_hook = None
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
        if self.complete_hook:
            self.complete_hook()
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
    change(engine, identifier, expires_at=(service.now() - timedelta(seconds=1)).isoformat())


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
    monkeypatch.setattr(gcs_storage, "_get_gcs_storage_client", lambda: signer)
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
    response = client.post(BASE + "/initiate", json={"filename": "another", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "completed"
    assert response.json()["upload_id"] is None
    assert response.json()["existing_file"]["mfid"] == file_id


@pytest.mark.parametrize("body", [{"filename": "../x", "size": 1}, {"filename": "/x", "size": 1}, {"filename": "x", "size": True}, {"filename": "x", "size": 0}, {"filename": "x", "size": 1, "sha256_hash": "bad"}])
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


def test_more_than_1000_parts_are_read_from_all_provider_pages(flow):
    _, engine, storage = flow
    identifier = start(flow)
    count = 1001
    crc = base64.b64encode(google_crc32c.Checksum(b"x" * count).digest()).decode()
    change(engine, identifier, part_size=1, part_count=count, size=count, claimed_sha256=None, part_manifest={"authorized": {str(n): CRC for n in range(1, count + 1)}})
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


def test_completion_aborts_duplicate_provider_uploads(flow):
    _, engine, storage = flow
    identifier = start(flow)
    put(flow, identifier)
    storage.initiate(row(engine, identifier))
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
def test_real_authentication_supports_both_credentials(flow, monkeypatch, credential_kind):
    from uuid import uuid4
    from sqlmodel import SQLModel
    from app.models.core_models import ApiToken, HubTokenFamily, HubRefreshToken, HubAccessToken
    from app.utils import auth_utils
    from app.utils.hub_tokens import issue_hub_tokens, refresh_hub_tokens, revoke_hub_tokens
    from app.api.main import app

    client, engine, storage = flow
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
        session.add(AssociatedFile(dataset_id=dataset.id, filename="reference", storage_path=f"{upload.bucket}/{upload.gcs_path}", size=len(DATA), sha256_hash=SHA, storage_generation=generation))
        session.commit()
    assert client.delete(BASE + f"/{identifier}").status_code == 204
    assert bool(storage.objects) == (generation is not None)


def test_unverified_multipart_hash_does_not_dedupe_legacy_paths(flow, monkeypatch):
    client, engine, _ = flow
    publish(flow)
    publish(flow, filename="copy.bin")
    response = client.post("/datasets/ds/files", json={"filename": "legacy.bin", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == "legacy.bin"
    monkeypatch.setattr("app.api.routes.gcs_storage.engine", engine)
    monkeypatch.setattr("app.api.routes.gcs_storage._mint_upload_token", lambda: ("token", "2030-01-01T00:00:00+00:00"))
    response = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "other.bin", "size": len(DATA), "sha256_hash": SHA})
    assert response.status_code == 200, response.text
    assert response.json()["existing_file"]["filename"] == "legacy.bin"


def test_legacy_paths_respect_multipart_filename_reservation(flow, monkeypatch):
    client, engine, _ = flow
    start(flow)
    response = client.post("/datasets/ds/files", json={"filename": "data.bin", "size": len(DATA), "sha256_hash": "1" * 64})
    assert response.status_code == 409
    monkeypatch.setattr("app.api.routes.gcs_storage.engine", engine)
    monkeypatch.setattr("app.api.routes.gcs_storage._mint_upload_token", lambda: ("token", "2030-01-01T00:00:00+00:00"))
    response = client.post("/datasets/ds/upload/initiate/multipart", json={"filename": "data.bin", "size": len(DATA)})
    assert response.status_code == 409
