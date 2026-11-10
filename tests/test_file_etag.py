import hashlib

from sqlmodel import Session

from app.tables import AssociatedFile, Dataset, Resource
from app.models.datasets import AssociatedFileRead, file_etag
from app.utils.permissions import PlatformRole

DS = "0t3qajrnzhtn7000x6s0x427hg"


def test_file_etag_formula():
    expected = '"' + hashlib.sha256(f"b/{DS}/a.txt|7|{'a' * 64}|10".encode()).hexdigest()[:32] + '"'
    assert file_etag(f"b/{DS}/a.txt", "7", "a" * 64, 10) == expected
    assert file_etag(f"b/{DS}/a.txt", 7, "a" * 64, 10) == expected
    assert file_etag(None, None, "a" * 64, 10) == '"' + hashlib.sha256(f"||{'a' * 64}|10".encode()).hexdigest()[:32] + '"'


def test_read_model_exposes_etag_but_not_generation():
    read = AssociatedFileRead.model_validate({"mfid": "m", "dataset_id": 1, "dataset_mfid": DS, "filename": "a.txt", "storage_path": f"b/{DS}/a.txt", "storage_backend": "gcs", "size": 10, "sha256_hash": "a" * 64, "storage_generation": "7"})
    body = read.model_dump()
    assert body["etag"] == file_etag(f"b/{DS}/a.txt", "7", "a" * 64, 10)
    assert "storage_generation" not in body


def _file(db_engine, **values):
    with Session(db_engine) as session:
        session.add(Resource(mfid=DS, resource_type="dataset"))
        dataset = Dataset(unique_id=DS, dataset_name="d", owner_orcid="0000-0000-0000-0001")
        session.add(dataset)
        session.commit()
        session.refresh(dataset)
        af = AssociatedFile(dataset_id=dataset.id, filename="a.txt", size=10, sha256_hash="a" * 64, storage_path=f"mf-storage-prod/{DS}/a.txt", **values)
        session.add(af)
        session.commit()
        session.refresh(af)
        return af.mfid


def test_get_file_returns_etag_header_and_field(client, db_engine, auth_as):
    file_mfid = _file(db_engine, storage_generation="7")
    auth_as("0000-0000-0000-0009", platform_role=PlatformRole.ADMIN)
    response = client.get(f"/files/{file_mfid}")
    assert response.status_code == 200, response.text
    expected = file_etag(f"mf-storage-prod/{DS}/a.txt", "7", "a" * 64, 10)
    assert response.headers["etag"] == expected
    assert response.json()["etag"] == expected


def test_etag_is_stable_across_rename(client, db_engine, auth_as):
    file_mfid = _file(db_engine)
    auth_as("0000-0000-0000-0009", platform_role=PlatformRole.ADMIN)
    before = client.get(f"/files/{file_mfid}").json()["etag"]
    renamed = client.patch(f"/files/{file_mfid}", json={"filename": "b.txt", "access_note": "x"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["etag"] == before
