from datetime import timedelta
from unittest.mock import patch

import pytest

from app.services import workers
from app.utils.permissions import PlatformRole
from tests.test_storage_download_routes import DS, _file, _switch

ADMIN, OTHER_ADMIN = "0000-0000-0000-00a1", "0000-0000-0000-00a2"


def register(client, worker_id, capabilities, buckets=("mf-storage-prod",)):
    entries = [entry if isinstance(entry, dict) else {"name": entry} for entry in capabilities]
    return client.put(f"/workers/{worker_id}", json={"kind": "ingestion", "capabilities": entries, "buckets": list(buckets), "worker_version": "0.4.1", "package_version": "0.2.1"})


@pytest.fixture
def admin(auth_as):
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN, is_service_account=True)
    return auth_as


@pytest.fixture
def clock(monkeypatch):
    start = workers.now()
    offset = {"value": timedelta()}
    monkeypatch.setattr(workers, "now", lambda: start + offset["value"])
    return offset


def test_live_workers_advertise_their_ingestors(client, admin, clock, monkeypatch):
    first = register(client, "worker-a", ["H5Ingestor", "TifIngestor"])
    assert first.status_code == 200, first.text
    assert first.json()["heartbeat_seconds"] < workers.TTL.total_seconds()
    clock["value"] = timedelta(minutes=5)
    assert register(client, "worker-b", ["MrcIngestor", "H5Ingestor"]).status_code == 200
    assert client.get("/ingestors").json() == ["H5Ingestor", "MrcIngestor", "TifIngestor"]
    clock["value"] = timedelta(minutes=11)
    assert client.get("/ingestors").json() == ["H5Ingestor", "MrcIngestor"]
    listed = {w["worker_id"]: (w["live"], w["store_ids"]) for w in client.get("/workers", params={"kind": "ingestion"}).json()}
    assert listed == {"worker-a": (False, ["mf-storage-prod"]), "worker-b": (True, ["mf-storage-prod"])}
    assert [w["worker_id"] for w in client.get("/workers", params={"live": "true"}).json()] == ["worker-b"]
    assert [w["worker_id"] for w in client.get("/workers", params={"live": "false"}).json()] == ["worker-a"]
    assert client.delete("/workers/worker-b").status_code == 204
    assert client.delete("/workers/worker-b").status_code == 204
    assert client.get("/ingestors").json() == []
    assert register(client, "worker-b", ["MrcIngestor"]).status_code == 200
    _switch(monkeypatch, "INGESTION_ENABLED", "false")
    assert client.get("/ingestors").json() == []


def test_a_worker_id_belongs_to_the_account_that_registered_it_while_live(client, admin, clock):
    assert register(client, "worker-a", ["H5Ingestor"]).status_code == 200
    admin(OTHER_ADMIN, platform_role=PlatformRole.ADMIN, is_service_account=True)
    taken = register(client, "worker-a", ["TifIngestor"])
    assert (taken.status_code, taken.json()["detail"]["code"]) == (409, "worker_id_taken")
    clock["value"] = timedelta(minutes=11)
    assert register(client, "worker-a", ["TifIngestor"]).status_code == 200


@pytest.mark.parametrize("role, service_account, can_register, can_list", [
    (PlatformRole.NONE, True, True, False),
    (PlatformRole.ADMIN, False, False, True),
    (PlatformRole.SUPPORT, False, False, True),
    (PlatformRole.CONTRIBUTOR, False, False, False),
])
def test_only_service_accounts_register_and_admins_or_support_list(client, auth_as, role, service_account, can_register, can_list):
    auth_as(ADMIN, platform_role=role, is_service_account=service_account)
    assert register(client, "worker-a", ["H5Ingestor"]).status_code == (200 if can_register else 403)
    assert client.delete("/workers/worker-a").status_code == (204 if can_register else 403)
    assert client.get("/workers").status_code == (200 if can_list else 403)


def test_ingest_warns_when_no_live_worker_can_take_the_request(client, db_engine, admin, clock):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    with patch("app.api.routes.files.send_pika_message") as send:
        nobody = client.post(f"/files/{file_mfid}/ingest")
        register(client, "worker-a", ["H5Ingestor"], buckets=["other-bucket"])
        wrong = client.post(f"/files/{file_mfid}/ingest", params={"ingestion_class": "TifIngestor"})
        register(client, "worker-b", ["TifIngestor"])
        fine = client.post(f"/files/{file_mfid}/ingest", params={"ingestion_class": "TifIngestor"})
    assert [response.status_code for response in (nobody, wrong, fine)] == [200, 200, 200]
    assert send.call_count == 3
    assert [w["code"] for w in nobody.json()["warnings"]] == ["no_live_worker"]
    assert [(w["code"], w.get("ingestion_class"), w.get("bucket")) for w in wrong.json()["warnings"]] == [("no_live_worker", "TifIngestor", None), ("store_not_read", None, "mf-storage-prod")]
    assert fine.json()["warnings"] == []


def test_ingestor_details_describe_and_suggest_by_file_name(client, admin, clock):
    assert register(client, "worker-a", [{"name": "MrcIngestor", "description": "old", "file_extensions": [".MRC"]}, "H5Ingestor"]).status_code == 200
    clock["value"] = timedelta(minutes=1)
    assert register(client, "worker-b", [{"name": "MrcIngestor", "description": "MRC maps", "file_extensions": [".mrc", ".map"]}, {"name": "TarIngestor", "file_extensions": [".tar.gz"]}]).status_code == 200
    details = {entry["name"]: (entry["description"], entry["file_extensions"]) for entry in client.get("/ingestors/details").json()}
    assert details == {"H5Ingestor": (None, []), "MrcIngestor": ("MRC maps", [".map", ".mrc"]), "TarIngestor": (None, [".tar.gz"])}
    assert [entry["name"] for entry in client.get("/ingestors/details", params={"file_name": "run/Scan.MRC"}).json()] == ["MrcIngestor"]
    assert [entry["name"] for entry in client.get("/ingestors/details", params={"file_name": "data.tar.gz"}).json()] == ["TarIngestor"]
    assert client.get("/ingestors").json() == ["H5Ingestor", "MrcIngestor", "TarIngestor"]
    assert register(client, "worker-c", [{"name": "X", "file_extensions": ["mrc"]}]).status_code == 422
