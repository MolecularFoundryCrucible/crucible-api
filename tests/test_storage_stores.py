import hashlib
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
from app.tables import AssociatedFile, Dataset, FileVersion, Instrument, Project, ResourceSettings, UploadSession
from app.models.multipart import MultipartInitiate
from app.storage.registry import COMPATIBILITY_STORE_ID
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


def test_stores_are_listed_without_endpoints_or_secrets(stores):
    client = stores[0]
    listed = client.get("/storage/stores")
    assert listed.status_code == 200, listed.text
    assert [set(item) for item in listed.json()] == [{"id", "display_name", "description", "default"}] * 2
    assert [(item["id"], item["default"]) for item in listed.json()] == [("default", True), ("vm", False)]


def test_file_responses_name_their_store(stores):
    client, engine, known, _ = stores
    file = complete((client, engine, known["default"]), publish((client, engine, known["default"]))).json()
    assert file["store"] == "default"
    move_to(engine, file["mfid"], "vm", known["vm"], known["default"])
    assert client.get(f"/files/{file['mfid']}").json()["store"] == "vm"
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file["mfid"])).one()
        record.store_id = None
        session.add(record)
        dataset_id = record.dataset_id
        session.add(AssociatedFile(dataset_id=dataset_id, filename="remote.h5", storage_backend="nersc-perlmutter", storage_path="/global/cfs/x", size=1, sha256_hash="b" * 64))
        session.commit()
    by_name = {item["filename"]: item["store"] for item in client.get("/datasets/ds/files").json()}
    assert by_name == {"data.bin": "default", "remote.h5": None}


BASE = "/datasets/ds/upload/multipart"
STORES_WITH_LAB = STORES + [{"id": "lab", "display_name": "Lab NAS", "type": "s3", "bucket": "dedicated", "endpoint_url": "http://nas.test:7070"}]


def defaults(engine, project=None, instrument=None):
    """Give dataset ds a project and an instrument, each with the given default store (None: no setting)."""
    with Session(engine) as session:
        session.add_all([Project(unique_id="pj", project_id="pj", organization="x", project_lead_orcid="owner"), Instrument(unique_id="in1", instrument_name="TEM", owner="owner", location="x")])
        for mfid, store in (("pj", project), ("in1", instrument)):
            if store is not None:
                session.add(ResourceSettings(mfid=mfid, settings={"default_store": store}, updated_at=service.now()))
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == "ds")).one()
        dataset.project_mfid, dataset.instrument_mfid = "pj", "in1"
        session.add(dataset)
        session.commit()


def initiate(client, store=None, filename="data.bin"):
    body = {"filename": filename, "size": len(DATA), "sha256_hash": SHA} | ({"store": store} if store else {})
    return client.post(BASE + "/initiate", json=body, headers={"Idempotency-Key": filename})


def session_store(engine, upload_id):
    with Session(engine) as session:
        return session.exec(select(UploadSession.store_id).where(UploadSession.upload_id == upload_id)).one()


@pytest.mark.parametrize("requested, project, instrument, expected", [
    ("lab", "vm", "default", "lab"),
    (None, "vm", "lab", "vm"),
    (None, None, "lab", "lab"),
    (None, None, None, "default"),
])
def test_the_store_follows_upload_then_project_then_instrument_then_deployment(stores, monkeypatch, requested, project, instrument, expected):
    client, engine, known, _ = stores
    known["lab"] = MemoryStorage(name="nas.test")
    monkeypatch.setenv("STORAGE_STORES", json.dumps(STORES_WITH_LAB))
    monkeypatch.setenv("STORAGE_LAB_ACCESS_KEY_ID", "lab-key")
    monkeypatch.setenv("STORAGE_LAB_SECRET_ACCESS_KEY", "lab-secret")
    get_settings.cache_clear()
    defaults(engine, project, instrument)
    response = initiate(client, requested)
    assert response.status_code == 200, response.text
    assert session_store(engine, response.json()["upload_id"]) == expected


def test_a_default_naming_a_removed_store_falls_through(stores):
    client, engine, _, _ = stores
    defaults(engine, project="gone", instrument="vm")
    response = initiate(client)
    assert response.status_code == 200, response.text
    assert session_store(engine, response.json()["upload_id"]) == "vm"


def test_an_upload_naming_an_unknown_store_is_refused(stores):
    response = initiate(stores[0], "gone")
    assert response.status_code == 422
    assert {key: response.json()["detail"][key] for key in ("code", "store")} == {"code": "unknown_store", "store": "gone"}


def test_a_replace_keeps_the_files_store_unless_another_is_named(stores):
    client, engine, known, _ = stores
    file = complete((client, engine, known["default"]), publish((client, engine, known["default"]))).json()
    move_to(engine, file["mfid"], "vm", known["vm"], known["default"])
    kept = client.post(f"/files/{file['mfid']}/upload/multipart/initiate", json={"size": len(DATA)})
    assert kept.status_code == 200, kept.text
    assert session_store(engine, kept.json()["upload_id"]) == "vm"
    assert client.delete(BASE + f"/{kept.json()['upload_id']}").status_code == 204
    moved = client.post(f"/files/{file['mfid']}/upload/multipart/initiate", json={"size": len(DATA), "store": "default"})
    assert moved.status_code == 200, moved.text
    assert session_store(engine, moved.json()["upload_id"]) == "default"


def test_the_request_fingerprint_without_store_is_unchanged():
    body = MultipartInitiate(filename="a", size=1)
    before = json.dumps({"filename": "a", "size": 1, "sha256_hash": None}, sort_keys=True, separators=(",", ":"))
    assert service.request_fingerprint(body) == hashlib.sha256(before.encode()).hexdigest()
    assert service.request_fingerprint(MultipartInitiate(filename="a", size=1, store="vm")) != service.request_fingerprint(body)
    assert COMPATIBILITY_STORE_ID == "default"


def test_storage_check_runs_against_each_store(stores, monkeypatch, capsys):
    from app.cli import storage as storage_cli
    from app.cli.__main__ import main
    checked = []

    class Recording:
        def __init__(self, store, bucket, record_bucket, part_bytes, keep, out=print):
            checked.append((store.name if hasattr(store, "name") else type(store).__name__, bucket, record_bucket))

        def run(self):
            return True

    monkeypatch.setattr(storage_cli, "StorageCheck", Recording)
    monkeypatch.setattr(storage_cli, "check_store", lambda store, public: stores[2][store.id])
    monkeypatch.setenv("RECORD_BUCKET", "records")
    get_settings.cache_clear()
    assert main(["storage", "check"]) == 0
    assert checked == [("storage.example", "dedicated", "records"), ("vm.test", "dedicated", None)]
    checked.clear()
    assert main(["storage", "check", "--store", "vm"]) == 0
    assert checked == [("vm.test", "dedicated", None)]
    assert main(["storage", "check", "--store", "gone"]) == 1


def test_the_store_listing_reports_files_in_unconfigured_stores(stores, capsys):
    from app.cli.__main__ import main
    client, engine, known, _ = stores
    kept = complete((client, engine, known["default"]), publish((client, engine, known["default"]), "a.bin")).json()
    lost = complete((client, engine, known["default"]), publish((client, engine, known["default"]), "b.bin")).json()
    move_to(engine, lost["mfid"], "gone", None, None)
    with Session(engine) as session:
        record = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == kept["mfid"])).one()
        record.store_id = None
        session.add(record)
        session.commit()
    capsys.readouterr()
    assert main(["storage", "stores"]) == 1
    output = capsys.readouterr().out
    assert "default  configured, default  1 files" in output
    assert "vm  configured  0 files" in output
    assert "gone  NOT CONFIGURED  1 files" in output
