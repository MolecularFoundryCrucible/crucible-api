import json
from datetime import timedelta

import pytest
from sqlmodel import Session, select

import app.api.routes.deleted_files as deleted_files_routes
import app.api.routes.gcs_storage as gcs_storage
import app.api.routes.multipart as multipart_routes
from app.config import get_settings
from app.services import multipart as service
from app.services.file_versions import restore_latest
from app.services.storage_cleanup import run_cleanup
from app.storage import StoreUnavailable
from app.tables import AssociatedFile, Dataset, FileVersion, UploadSession
from tests.test_multipart_lifecycle import DATA, SHA, MemoryStorage, complete, put, publish, replace, start
import tests.test_multipart_lifecycle as lifecycle

flow = lifecycle.flow
STORES = [
    {"id": "default", "display_name": "Foundry storage", "type": "gcs", "bucket": "dedicated"},
    {"id": "vm", "display_name": "Lab VM", "type": "s3", "bucket": "dedicated", "endpoint_url": "http://vm.test:9000"},
]


@pytest.fixture
def stores(flow, monkeypatch):
    client, engine, default = flow
    known = {"default": default, "vm": MemoryStorage(name="vm.test")}

    def lookup(store_id=None):
        if (store_id or "default") not in known:
            raise StoreUnavailable(store_id)
        return known[store_id or "default"]

    for module in (multipart_routes, gcs_storage, deleted_files_routes):
        monkeypatch.setattr(module, "get_store", lookup)
    monkeypatch.delenv("UPLOAD_BUCKET")
    monkeypatch.delenv("MULTIPART_UPLOAD_BUCKET")
    monkeypatch.setenv("STORAGE_STORES", json.dumps(STORES))
    monkeypatch.setenv("STORAGE_DEFAULT_STORE", "default")
    monkeypatch.setenv("STORAGE_VM_ACCESS_KEY_ID", "vm-key")
    monkeypatch.setenv("STORAGE_VM_SECRET_ACCESS_KEY", "vm-secret")
    get_settings.cache_clear()
    return client, engine, known, lookup


def move_to(engine, mfid, store_id, storage, source):
    """Put a published file's object in another store, as an upload to that store would have."""
    with Session(engine) as session:
        file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == mfid)).one()
        file.store_id = store_id
        session.add(file)
        session.commit()
        key = file.storage_path.partition("/")[2]
    if storage is not None:
        storage.objects[key] = source.objects.pop(key)
    return key


def test_a_file_is_downloaded_from_its_own_store(stores):
    client, engine, known, _ = stores
    file = complete(stores[:2] + (known["default"],), publish((client, engine, known["default"]))).json()
    move_to(engine, file["mfid"], "vm", known["vm"], known["default"])
    url = client.get(f"/files/{file['mfid']}/download_link").json()["url"]
    assert url.startswith("https://vm.test/dedicated/")


def test_a_file_in_an_unconfigured_store_answers_503(stores):
    client, engine, known, _ = stores
    file = complete((client, engine, known["default"]), publish((client, engine, known["default"]))).json()
    move_to(engine, file["mfid"], "gone", None, None)
    response = client.get(f"/files/{file['mfid']}/download_link")
    assert response.status_code == 503
    assert {key: response.json()["detail"][key] for key in ("code", "store")} == {"code": "store_unavailable", "store": "gone"}
    assert client.get(f"/files/{file['mfid']}").status_code == 200


def test_batch_download_links_skip_an_unavailable_store(stores):
    client, engine, known, _ = stores
    kept = complete((client, engine, known["default"]), publish((client, engine, known["default"]), "a.bin")).json()
    lost = complete((client, engine, known["default"]), publish((client, engine, known["default"]), "b.bin")).json()
    move_to(engine, lost["mfid"], "gone", None, None)
    links = client.get("/datasets/ds/download_links")
    assert links.status_code == 200
    assert links.json()[kept["mfid"]].startswith("https://storage.example/")
    assert links.json()[lost["mfid"]] is None


def test_cleanup_deletes_versions_in_their_own_store_and_reports_an_unavailable_one(stores):
    client, engine, known, lookup = stores
    in_vm = complete((client, engine, known["default"]), publish((client, engine, known["default"]), "a.bin")).json()
    gone = complete((client, engine, known["default"]), publish((client, engine, known["default"]), "b.bin")).json()
    vm_key = move_to(engine, in_vm["mfid"], "vm", known["vm"], known["default"])
    move_to(engine, gone["mfid"], "gone", None, None)
    assert client.delete(f"/files/{in_vm['mfid']}").status_code == 200
    assert client.delete(f"/files/{gone['mfid']}").status_code == 200
    with Session(engine) as session:
        versions = {version.file_mfid: version for version in session.exec(select(FileVersion)).all()}
    assert set(versions) == {in_vm["mfid"]}
    with Session(engine) as session:
        session.add(FileVersion(file_mfid=gone["mfid"], dataset_mfid="ds", storage_path=gone["storage_path"], storage_generation="7", size=1, sha256_hash=SHA, reason="deleted", store_id="gone", created_at=service.now(), delete_after=service.now()))
        session.commit()
        gone_id = session.exec(select(FileVersion.id).where(FileVersion.store_id == "gone")).one()
    report = run_cleanup(engine, lookup, service.now() + timedelta(days=15))
    assert known["vm"].deleted == [(vm_key, "7")]
    assert report.failures == [f"version {gone_id}: store_unavailable"]
    with Session(engine) as session:
        assert session.get(FileVersion, gone_id).deleted_at is None


def test_a_session_started_before_the_upgrade_publishes_into_default(stores):
    client, engine, known, _ = stores
    identifier = start((client, engine, known["default"]))
    with Session(engine) as session:
        upload = session.exec(select(UploadSession).where(UploadSession.upload_id == identifier)).one()
        upload.store_id = None
        session.add(upload)
        session.commit()
    put((client, engine, known["default"]), identifier)
    response = complete((client, engine, known["default"]), identifier)
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert session.exec(select(AssociatedFile.store_id)).one() in (None, "default")


def test_new_uploads_record_their_store(stores):
    client, engine, known, _ = stores
    identifier = publish((client, engine, known["default"]))
    with Session(engine) as session:
        assert session.exec(select(UploadSession.store_id).where(UploadSession.upload_id == identifier)).one() == "default"
        assert session.exec(select(AssociatedFile.store_id)).one() == "default"


def test_replacing_and_restoring_stay_in_the_files_store(stores):
    client, engine, known, lookup = stores
    file = complete((client, engine, known["default"]), publish((client, engine, known["default"]))).json()
    move_to(engine, file["mfid"], "vm", known["vm"], known["default"])
    with Session(engine) as session:
        session.exec(select(Dataset)).one()
    _, response = replace((client, engine, known["vm"]), file["mfid"])
    assert response.status_code == 200, response.text
    with Session(engine) as session:
        assert session.exec(select(AssociatedFile.store_id)).one() == "vm"
        assert [version.store_id for version in session.exec(select(FileVersion)).all()] == ["vm"]
    restore_latest(engine, lookup, file["mfid"], service.now())
    with Session(engine) as session:
        assert session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file["mfid"])).one().storage_path == file["storage_path"]
