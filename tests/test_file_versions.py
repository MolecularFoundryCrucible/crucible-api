from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.tables import AssociatedFile, Dataset, DeletionRequest, FileVersion, IngestionRequest, UploadSession
from app.services import multipart as service
from app.services.file_versions import RestoreRefused, purge_deleted, restore_deleted, restore_latest
from app.services.storage_cleanup import run_cleanup
from app.storage import StorageFailure
from app.utils.deletion_utils import hard_delete_resource
from tests.test_multipart_lifecycle import BASE, CRC, DATA, SHA, change, files, published_file, put, replace, replace_start, row, start
import tests.test_multipart_lifecycle as lifecycle

flow = lifecycle.flow


def versions(engine):
    with Session(engine) as session:
        return session.exec(select(FileVersion).order_by(FileVersion.id)).all()


def stored_file(engine, mfid):
    with Session(engine) as session:
        return session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one()


def replaced(flow):
    file = published_file(flow)
    _, response = replace(flow, file["mfid"])
    assert response.status_code == 200, response.text
    return file, response.json()


def test_a_replace_keeps_the_previous_content_as_a_version(flow):
    _, engine, _ = flow
    file, current = replaced(flow)
    [version] = versions(engine)
    assert (version.file_mfid, version.dataset_mfid, version.storage_path, version.storage_generation) == (file["mfid"], "ds", file["storage_path"], "7")
    assert (version.size, version.sha256_hash, version.storage_crc32c, version.reason) == (len(DATA), SHA, CRC, "replaced")
    assert version.delete_after - version.created_at == timedelta(days=14)
    assert (version.deleted_at, version.restored_at, version.attempts) == (None, None, 0)
    assert current["storage_path"] != file["storage_path"]


def test_the_grace_period_comes_from_the_setting(flow, monkeypatch):
    monkeypatch.setenv("FILE_VERSION_RETENTION_DAYS", "3")
    from app.config import get_settings
    get_settings.cache_clear()
    replaced(flow)
    [version] = versions(flow[1])
    assert version.delete_after - version.created_at == timedelta(days=3)


def test_cleanup_deletes_a_version_only_after_its_grace_period(flow):
    _, engine, storage = flow
    file, _ = replaced(flow)
    key = file["storage_path"].partition("/")[2]
    early = run_cleanup(engine, storage, service.now() + timedelta(days=13))
    assert (early.deleted_versions, storage.deleted) == (0, [])
    report = run_cleanup(engine, storage, service.now() + timedelta(days=15))
    assert (report.deleted_versions, report.failures) == (1, [])
    assert storage.deleted == [(key, "7")]
    assert versions(engine)[0].deleted_at is not None
    assert run_cleanup(engine, storage, service.now() + timedelta(days=16)).deleted_versions == 0


def test_cleanup_keeps_a_version_another_file_still_uses(flow):
    _, engine, storage = flow
    file, _ = replaced(flow)
    with Session(engine) as session:
        dataset_id = session.exec(select(AssociatedFile.dataset_id)).first()
        session.add(AssociatedFile(dataset_id=dataset_id, filename="copy.bin", storage_path=file["storage_path"], size=1, sha256_hash="b" * 64))
        session.commit()
    report = run_cleanup(engine, storage, service.now() + timedelta(days=15))
    assert (report.deleted_versions, report.kept_in_use, storage.deleted) == (0, 1, [])
    assert versions(engine)[0].deleted_at is None


def test_a_dry_run_changes_nothing(flow):
    _, engine, storage = flow
    replaced(flow)
    report = run_cleanup(engine, storage, service.now() + timedelta(days=15), dry_run=True)
    assert report.deleted_versions == 1
    assert storage.deleted == []
    assert versions(engine)[0].deleted_at is None


def test_a_storage_failure_is_counted_and_the_other_versions_still_go(flow, monkeypatch):
    client, engine, storage = flow
    first, _ = replaced(flow)
    second = published_file(flow, "other.bin")
    _, response = replace(flow, second["mfid"])
    assert response.status_code == 200, response.text
    real_delete = storage.delete
    failing = first["storage_path"].partition("/")[2]

    def delete(bucket, key, version):
        if key == failing:
            raise StorageFailure("provider_unavailable")
        real_delete(bucket, key, version)
    monkeypatch.setattr(storage, "delete", delete)
    report = run_cleanup(engine, storage, service.now() + timedelta(days=15))
    assert report.deleted_versions == 1
    assert report.failures == [f"version {versions(engine)[0].id}: provider_unavailable"]
    failed = versions(engine)[0]
    assert (failed.attempts, failed.failure_code, failed.deleted_at) == (1, "provider_unavailable", None)


def test_cleanup_aborts_an_expired_upload_once_and_leaves_live_ones(flow):
    _, engine, storage = flow
    stale = start(flow, "stale.bin")
    put(flow, stale)
    live = start(flow, "live.bin")
    change(engine, stale, expires_at=service.now() - timedelta(hours=2))
    report = run_cleanup(engine, storage, service.now())
    assert report.aborted_uploads == 1
    assert row(engine, stale).state == "expired" and row(engine, stale).cleaned_at is not None
    assert row(engine, live).state == "uploading" and row(engine, live).cleaned_at is None
    assert run_cleanup(engine, storage, service.now()).aborted_uploads == 0


def test_restore_brings_back_the_previous_content_and_can_be_undone(flow):
    _, engine, storage = flow
    file, current = replaced(flow)
    restored = restore_latest(engine, storage, file["mfid"], service.now())
    after = stored_file(engine, file["mfid"])
    assert (after.storage_path, after.storage_generation, after.size, after.sha256_hash) == (file["storage_path"], "7", len(DATA), SHA)
    first, second = versions(engine)
    assert first.restored_at is not None and restored.version_id == first.id
    assert (second.storage_path, restored.replaced_version_id) == (current["storage_path"], second.id)
    restore_latest(engine, storage, file["mfid"], service.now())
    assert stored_file(engine, file["mfid"]).storage_path == current["storage_path"]
    assert run_cleanup(engine, storage, service.now() + timedelta(days=15)).deleted_versions == 1
    assert stored_file(engine, file["mfid"]).storage_path == current["storage_path"]


@pytest.mark.parametrize("situation, code", [("never_replaced", "no_kept_version"), ("object_gone", "version_missing_in_storage"), ("replace_running", "replace_in_progress"), ("unknown_file", "file_not_found")])
def test_restore_refuses_when_it_cannot_be_done(flow, situation, code):
    _, engine, storage = flow
    if situation == "never_replaced":
        mfid = published_file(flow)["mfid"]
    elif situation == "object_gone":
        file, _ = replaced(flow)
        mfid = file["mfid"]
        storage.objects.pop(file["storage_path"].partition("/")[2])
    elif situation == "replace_running":
        file, _ = replaced(flow)
        mfid = file["mfid"]
        assert replace_start(flow, mfid).status_code == 200
    else:
        mfid = "no-such-file"
    with pytest.raises(RestoreRefused) as refused:
        restore_latest(engine, storage, mfid, service.now())
    assert refused.value.code == code


def test_only_one_waiting_version_per_stored_object(flow):
    _, engine, _ = flow
    with Session(engine) as session:
        for _ in range(2):
            session.add(FileVersion(file_mfid="f", dataset_mfid="ds", storage_path="dedicated/ds/_files/x", size=1, sha256_hash="a" * 64, reason="replaced", created_at=service.now(), delete_after=service.now()))
        with pytest.raises(Exception):
            session.commit()


def test_upload_sessions_are_untouched_by_version_cleanup(flow):
    _, engine, storage = flow
    replaced(flow)
    with Session(engine) as session:
        before = len(session.exec(select(UploadSession)).all())
    run_cleanup(engine, storage, service.now() + timedelta(days=15))
    with Session(engine) as session:
        assert len(session.exec(select(UploadSession)).all()) == before
    assert len(files(engine)) == 1


def due_version(engine, storage, path, used_by=None):
    """A version past its grace period, with its object in the store, optionally still used by a file."""
    with Session(engine) as session:
        if used_by is not None:
            dataset_id = session.exec(select(AssociatedFile.dataset_id)).first()
            session.add(AssociatedFile(dataset_id=dataset_id, filename=used_by, storage_path=path, size=1, sha256_hash="b" * 64))
        version = FileVersion(file_mfid="f", dataset_mfid="ds", storage_path=path, storage_generation="7", size=1, sha256_hash="a" * 64,
                              reason="replaced", created_at=service.now() - timedelta(days=20), delete_after=service.now() - timedelta(days=6))
        session.add(version)
        session.commit()
        session.refresh(version)
    storage.objects[path.partition("/")[2]] = {"generation": "7", "size": 1, "crc32c": "", "data": b"x"}
    return version.id


def test_a_dry_run_writes_nothing_for_a_version_outside_the_own_buckets(flow):
    _, engine, storage = flow
    published_file(flow)
    with Session(engine) as session:
        session.add(FileVersion(file_mfid="f", dataset_mfid="ds", storage_path="elsewhere/ds/_files/x", storage_generation="7", size=1, sha256_hash="a" * 64,
                                reason="replaced", created_at=service.now(), delete_after=service.now() - timedelta(days=1)))
        session.commit()
    report = run_cleanup(engine, storage, service.now(), dry_run=True)
    assert report.failures == [f"version {versions(engine)[0].id}: not_owned"]
    assert versions(engine)[0].failure_code is None
    run_cleanup(engine, storage, service.now())
    assert versions(engine)[0].failure_code == "not_owned"


def test_versions_still_in_use_never_hold_back_a_deletable_one(flow):
    _, engine, storage = flow
    published_file(flow)
    for n in range(3):
        due_version(engine, storage, f"dedicated/ds/_files/used{n}", used_by=f"copy{n}.bin")
    free = due_version(engine, storage, "dedicated/ds/_files/free")
    report = run_cleanup(engine, storage, service.now(), batch=2)
    assert (report.kept_in_use, report.deleted_versions) == (3, 1)
    assert [version.id for version in versions(engine) if version.deleted_at is not None] == [free]


def test_one_failing_upload_does_not_block_the_others(flow, monkeypatch):
    _, engine, storage = flow
    first, second = start(flow, "first.bin"), start(flow, "second.bin")
    for identifier in (first, second):
        change(engine, identifier, expires_at=service.now() - timedelta(hours=2))
    failing = row(engine, first).object_key
    real = storage.open_uploads

    def open_uploads(bucket, key):
        if key == failing:
            raise StorageFailure("provider_unavailable")
        return real(bucket, key)
    monkeypatch.setattr(storage, "open_uploads", open_uploads)
    report = run_cleanup(engine, storage, service.now())
    assert report.aborted_uploads == 1
    assert report.failures == [f"upload {first}: provider_unavailable"]
    assert row(engine, second).cleaned_at is not None and row(engine, first).cleaned_at is None


def test_an_upload_finishing_its_completion_is_left_alone_just_after_expiry(flow):
    _, engine, storage = flow
    finishing = start(flow, "finishing.bin")
    change(engine, finishing, state="finalizing", expires_at=service.now() - timedelta(minutes=10))
    assert run_cleanup(engine, storage, service.now()).aborted_uploads == 0
    assert row(engine, finishing).state == "finalizing"
    change(engine, finishing, expires_at=service.now() - timedelta(hours=2))
    assert run_cleanup(engine, storage, service.now()).aborted_uploads == 1
    assert row(engine, finishing).state == "expired"


def test_a_second_file_sharing_the_object_gets_its_full_grace_period(flow):
    _, engine, _ = flow
    file = published_file(flow)
    with Session(engine) as session:
        dataset_id = session.exec(select(AssociatedFile.dataset_id)).first()
        twin = AssociatedFile(dataset_id=dataset_id, filename="twin.bin", storage_path=file["storage_path"], storage_generation="7", size=len(DATA), sha256_hash=SHA)
        session.add(twin)
        session.commit()
        twin_mfid = twin.mfid
    _, response = replace(flow, file["mfid"])
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        version = session.exec(select(FileVersion)).one()
        version.delete_after = service.now() - timedelta(hours=1)
        session.add(version)
        session.commit()
    _, response = replace(flow, twin_mfid)
    assert response.status_code == 200, response.text
    [version] = versions(engine)
    assert service.utc(version.delete_after) > service.now() + timedelta(days=13)


@pytest.mark.parametrize("situation, code", [("past_grace", "version_expired"), ("other_size", "version_size_mismatch")])
def test_restore_refuses_an_expired_or_changed_version(flow, situation, code):
    _, engine, storage = flow
    file, _ = replaced(flow)
    if situation == "past_grace":
        with Session(engine) as session:
            version = session.exec(select(FileVersion)).one()
            version.delete_after = service.now() - timedelta(minutes=1)
            session.add(version)
            session.commit()
    else:
        storage.objects[file["storage_path"].partition("/")[2]]["size"] += 1
    with pytest.raises(RestoreRefused) as refused:
        restore_latest(engine, storage, file["mfid"], service.now())
    assert refused.value.code == code
    assert stored_file(engine, file["mfid"]).storage_path != file["storage_path"]


@pytest.mark.parametrize("enabled, dry_run, runs", [(None, False, False), ("false", False, False), ("true", False, True), (None, True, True)])
def test_cleanup_command_runs_only_when_enabled_or_dry(monkeypatch, capsys, enabled, dry_run, runs):
    from app.cli.__main__ import main
    calls = []
    monkeypatch.setattr("app.config.check_settings", lambda: None)
    monkeypatch.setattr("app.storage.get_store", lambda *_: object())
    monkeypatch.setattr("app.api.dependencies.get_engine", lambda: object())
    monkeypatch.setattr("app.services.storage_cleanup.run_cleanup", lambda engine, store, now, dry_run, batch: calls.append(dry_run) or lifecycle_report())
    if enabled is None:
        monkeypatch.delenv("STORAGE_CLEANUP_ENABLED", raising=False)
    else:
        monkeypatch.setenv("STORAGE_CLEANUP_ENABLED", enabled)
    assert main(["storage", "cleanup"] + (["--dry-run"] if dry_run else [])) == 0
    assert calls == ([dry_run] if runs else [])
    if not runs:
        assert "disabled" in capsys.readouterr().out


def lifecycle_report():
    from app.services.storage_cleanup import CleanupReport
    return CleanupReport()


def ingestion_request(engine, file_mfid, **values):
    with Session(engine) as session:
        request = IngestionRequest(dataset_mfid="ds", file_mfid=file_mfid, file_to_upload="dedicated/ds/x", ingestion_class="Reader", status="completed", time_created="2026-11-01T00:00:00Z", **values)
        session.add(request)
        session.commit()
        session.refresh(request)
        return request.model_dump()


def deleted(flow, filename="data.bin"):
    file = published_file(flow, filename)
    response = flow[0].delete(f"/files/{file['mfid']}")
    assert response.status_code == 200, response.text
    return file


def test_deleting_a_file_puts_its_content_in_the_trash(flow):
    _, engine, storage = flow
    file = deleted(flow)
    [version] = versions(engine)
    assert (version.reason, version.filename, version.file_mfid, version.dataset_mfid) == ("deleted", "data.bin", file["mfid"], "ds")
    assert (version.storage_path, version.storage_generation, version.size, version.sha256_hash) == (file["storage_path"], "7", len(DATA), SHA)
    assert version.delete_after - version.created_at == timedelta(days=14)
    assert file["storage_path"].partition("/")[2] in storage.objects


def test_deleting_a_file_keeps_its_ingestion_requests_in_the_version(flow):
    client, engine, _ = flow
    file = published_file(flow)
    request = ingestion_request(engine, file["mfid"], time_completed="2026-11-02T00:00:00Z", ingestion_githash="abc")
    assert client.delete(f"/files/{file['mfid']}").status_code == 200
    with Session(engine) as session:
        assert session.exec(select(IngestionRequest)).all() == []
    assert versions(engine)[0].ingestion_requests == [request]


def test_cleanup_removes_trashed_content_after_the_grace_period(flow):
    _, engine, storage = flow
    file = deleted(flow)
    key = file["storage_path"].partition("/")[2]
    assert run_cleanup(engine, storage, service.now() + timedelta(days=13)).deleted_versions == 0
    assert storage.deleted == []
    assert run_cleanup(engine, storage, service.now() + timedelta(days=15)).deleted_versions == 1
    assert storage.deleted == [(key, "7")]


def test_deleting_a_file_during_its_replace_is_refused(flow):
    client, engine, _ = flow
    file = published_file(flow)
    upload_id = replace_start(flow, file["mfid"]).json()["upload_id"]
    response = client.delete(f"/files/{file['mfid']}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "replace_in_progress"
    assert response.json()["detail"]["upload_id"] == upload_id
    assert stored_file(engine, file["mfid"]).storage_path == file["storage_path"]
    assert versions(engine) == []


def test_hard_deleting_a_dataset_trashes_every_file(flow):
    _, engine, _ = flow
    first = published_file(flow, "a.bin")
    second = published_file(flow, "b.bin")
    request = ingestion_request(engine, first["mfid"])
    with Session(engine) as session:
        hard_delete_resource("ds", session)
        session.commit()
    trashed = {version.file_mfid: version for version in versions(engine)}
    assert {mfid: (version.reason, version.filename) for mfid, version in trashed.items()} == {first["mfid"]: ("deleted", "a.bin"), second["mfid"]: ("deleted", "b.bin")}
    assert trashed[first["mfid"]].ingestion_requests == [request]


def test_trashed_content_another_record_uses_is_kept(flow):
    _, engine, storage = flow
    file = published_file(flow)
    with Session(engine) as session:
        dataset_id = session.exec(select(AssociatedFile.dataset_id)).first()
        session.add(AssociatedFile(dataset_id=dataset_id, filename="copy.bin", storage_path=file["storage_path"], size=1, sha256_hash="b" * 64))
        session.commit()
    assert flow[0].delete(f"/files/{file['mfid']}").status_code == 200
    report = run_cleanup(engine, storage, service.now() + timedelta(days=15))
    assert (report.deleted_versions, report.kept_in_use, storage.deleted) == (0, 1, [])


def test_deleting_a_legacy_file_without_generation_trashes_nothing(flow):
    client, engine, storage = flow
    file = published_file(flow)
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file["mfid"])).one()
        record.storage_generation = None
        session.add(record)
        session.commit()
    assert client.delete(f"/files/{file['mfid']}").status_code == 200
    assert versions(engine) == []
    assert run_cleanup(engine, storage, service.now() + timedelta(days=15)).deleted_versions == 0


def snapshot(engine):
    return sorted((file.mfid, file.filename, file.storage_path) for file in files(engine)), [(version.id, version.restored_at, version.delete_after) for version in versions(engine)]


def test_restoring_a_deleted_file_brings_it_back_with_the_same_mfid(flow):
    _, engine, storage = flow
    file = deleted(flow)
    restored = restore_deleted(engine, storage, "ds", file["mfid"], service.now())
    assert restored.mfid == file["mfid"]
    after = stored_file(engine, file["mfid"])
    assert (after.filename, after.storage_path, after.storage_generation, after.size, after.sha256_hash, after.storage_crc32c, after.sha256_verified_at) == ("data.bin", file["storage_path"], "7", len(DATA), SHA, CRC, None)
    assert versions(engine)[0].restored_at is not None
    assert run_cleanup(engine, storage, service.now() + timedelta(days=15)).deleted_versions == 0


def ingestion_requests(engine):
    with Session(engine) as session:
        return [row.model_dump() for row in session.exec(select(IngestionRequest).order_by(IngestionRequest.id)).all()]


def test_restoring_a_deleted_file_brings_back_its_ingestion_requests(flow):
    client, engine, storage = flow
    other = published_file(flow, "other.bin")
    unrelated = ingestion_request(engine, other["mfid"])
    file = published_file(flow)
    request = ingestion_request(engine, file["mfid"], ingestion_githash="abc")
    assert client.delete(f"/files/{file['mfid']}").status_code == 200
    restore_deleted(engine, storage, "ds", file["mfid"], service.now())
    assert ingestion_requests(engine) == [unrelated, request]


def test_a_restored_ingestion_request_whose_id_was_reused_gets_a_new_one(flow):
    client, engine, storage = flow
    file = published_file(flow)
    request = ingestion_request(engine, file["mfid"], ingestion_githash="abc")
    other = published_file(flow, "other.bin")
    assert client.delete(f"/files/{file['mfid']}").status_code == 200
    with Session(engine) as session:
        session.add(IngestionRequest(id=request["id"], dataset_mfid="ds", file_mfid=other["mfid"], status="requested"))
        session.commit()
    restore_deleted(engine, storage, "ds", file["mfid"], service.now())
    [kept, restored] = ingestion_requests(engine)
    assert (kept["id"], kept["file_mfid"]) == (request["id"], other["mfid"])
    assert restored["id"] != request["id"]
    assert {**restored, "id": request["id"]} == request


@pytest.mark.parametrize("situation, code", [
    ("name_taken", "filename_exists"), ("name_reserved", "filename_reserved"), ("expired", "version_expired"), ("purged", "version_expired"),
    ("object_gone", "version_missing_in_storage"), ("pending_deletion", "dataset_pending_deletion"), ("never_deleted", "not_in_trash"),
])
def test_restore_of_a_deleted_file_refuses(flow, situation, code):
    _, engine, storage = flow
    when = service.now()
    if situation == "never_deleted":
        mfid = published_file(flow)["mfid"]
    else:
        file = deleted(flow)
        mfid = file["mfid"]
    if situation == "name_taken":
        with Session(engine) as session:
            dataset_id = session.exec(select(Dataset.id)).one()
            session.add(AssociatedFile(dataset_id=dataset_id, filename="data.bin", storage_path="dedicated/ds/other", size=1, sha256_hash="b" * 64))
            session.commit()
    elif situation == "name_reserved":
        response = flow[0].post(BASE + "/initiate", json={"filename": "data.bin", "size": len(DATA), "sha256_hash": SHA}, headers={"Idempotency-Key": "again"})
        assert response.status_code == 200, response.text
    elif situation == "expired":
        when = when + timedelta(days=15)
    elif situation == "purged":
        with Session(engine) as session:
            purge_deleted(session, "ds", mfid, when)
            session.commit()
    elif situation == "object_gone":
        storage.objects.pop(file["storage_path"].partition("/")[2])
    elif situation == "pending_deletion":
        with Session(engine) as session:
            session.add(DeletionRequest(resource_id="ds", resource_type="dataset", requester_id="owner", status="pending"))
            session.commit()
    before = snapshot(engine)
    with pytest.raises(RestoreRefused) as refused:
        restore_deleted(engine, storage, "ds", mfid, when)
    assert refused.value.code == code
    assert snapshot(engine) == before


def test_purging_a_deleted_file_lets_cleanup_remove_it_now(flow):
    _, engine, storage = flow
    file = deleted(flow)
    with Session(engine) as session:
        purge_deleted(session, "ds", file["mfid"], service.now())
        session.commit()
    assert run_cleanup(engine, storage, service.now() + timedelta(seconds=1)).deleted_versions == 1
    assert storage.deleted == [(file["storage_path"].partition("/")[2], "7")]


def test_a_replace_completed_while_the_file_is_being_deleted_is_trashed_too(flow, monkeypatch):
    import app.api.routes.files as file_routes
    client, engine, _ = flow
    file = published_file(flow)
    identifier = replace_start(flow, file["mfid"]).json()["upload_id"]
    put(flow, identifier)
    checked = file_routes.require_permission
    def completes_the_replace_first(*args, **kwargs):
        monkeypatch.setattr(file_routes, "require_permission", checked)
        assert lifecycle.complete(flow, identifier).status_code == 200
        return checked(*args, **kwargs)
    monkeypatch.setattr(file_routes, "require_permission", completes_the_replace_first)
    assert client.delete(f"/files/{file['mfid']}").status_code == 200
    assert sorted((version.reason, version.storage_path) for version in versions(engine)) == [("deleted", f"dedicated/ds/_files/{identifier}"), ("replaced", file["storage_path"])]
