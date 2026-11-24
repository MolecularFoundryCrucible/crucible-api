from datetime import timedelta

import pytest
from sqlmodel import Session, select

import app.api.routes.deleted_files as routes
from app.tables import AccessGroup, AssociatedFile, Dataset, FileVersion
from app.utils.permissions import Role
from tests.test_multipart_lifecycle import DATA, SHA, published_file
import tests.test_multipart_lifecycle as lifecycle

flow = lifecycle.flow
TRASH = "/datasets/ds/deleted_files"


@pytest.fixture
def trash(flow, monkeypatch):
    monkeypatch.setattr(routes, "get_store", flow[2])
    file = published_file(flow)
    assert flow[0].delete(f"/files/{file['mfid']}").status_code == 200
    return flow, file


def test_dataset_admin_lists_restores_and_purges_deleted_files(trash):
    (client, engine, _), file = trash
    listed = client.get(TRASH)
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert (item["file_mfid"], item["dataset_mfid"], item["filename"], item["size"], item["sha256_hash"]) == (file["mfid"], "ds", "data.bin", len(DATA), SHA)
    assert item["restorable_until"] > item["deleted_at"]
    restored = client.post(f"{TRASH}/{file['mfid']}/restore")
    assert restored.status_code == 200, restored.text
    assert (restored.json()["mfid"], restored.json()["etag"]) == (file["mfid"], file["etag"])
    assert client.get(TRASH).json()["items"] == []
    assert client.delete(f"/files/{file['mfid']}").status_code == 200
    purged = client.delete(f"{TRASH}/{file['mfid']}")
    assert purged.status_code == 200, purged.text
    with Session(engine) as session:
        [_, version] = session.exec(select(FileVersion).order_by(FileVersion.id)).all()
        assert version.delete_after <= version.created_at + timedelta(seconds=5)


@pytest.mark.parametrize("role", [Role.VIEWER, Role.EDITOR])
def test_only_dataset_admins_manage_deleted_files(trash, auth_as, role):
    (client, engine, _), file = trash
    with Session(engine) as session:
        group_id = session.exec(select(AccessGroup.id).where(AccessGroup.group_name == "owner-group")).one()
    auth_as("someone", groups=[("owner-group", group_id, role)])
    assert client.get(TRASH).status_code == 403
    assert client.post(f"{TRASH}/{file['mfid']}/restore").status_code == 403
    assert client.delete(f"{TRASH}/{file['mfid']}").status_code == 403


def test_restore_errors_carry_a_code(trash):
    (client, engine, _), file = trash
    with Session(engine) as session:
        dataset_id = session.exec(select(Dataset.id)).one()
        session.add(AssociatedFile(dataset_id=dataset_id, filename="data.bin", storage_path="dedicated/ds/other", size=1, sha256_hash="b" * 64))
        session.commit()
    taken = client.post(f"{TRASH}/{file['mfid']}/restore")
    assert (taken.status_code, taken.json()["detail"]["code"]) == (409, "filename_exists")
    assert client.delete(f"{TRASH}/{file['mfid']}").status_code == 200
    expired = client.post(f"{TRASH}/{file['mfid']}/restore")
    assert (expired.status_code, expired.json()["detail"]["code"]) == (409, "version_expired")
    unknown = client.post(f"{TRASH}/no-such-file/restore")
    assert (unknown.status_code, unknown.json()["detail"]["code"]) == (404, "not_in_trash")
    assert client.get("/datasets/no-such-dataset/deleted_files").status_code == 404


def test_a_purged_file_is_no_longer_listed(trash):
    (client, _, _), file = trash
    assert client.delete(f"{TRASH}/{file['mfid']}").status_code == 200
    assert client.get(TRASH).json() == {"total": 0, "limit": 100, "offset": 0, "items": [], "next_cursor": None}
