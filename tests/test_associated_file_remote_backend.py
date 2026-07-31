from unittest.mock import patch
from sqlmodel import Session
from app.models.core_models import Resource, Dataset, AssociatedFile, AccessGroup, ResourceACL
from app.models.datasets import AssociatedFileCreate, AssociatedFileRead, AssociatedFileUpdate


def _make_public_dataset_with_file(db_engine, dsid, storage_path="mf-storage-prod/x/f.txt"):
    with Session(db_engine) as session:
        session.add(Resource(mfid=dsid, resource_type="dataset", public=True))
        ds = Dataset(unique_id=dsid, dataset_name="test", public=True, owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)
        af = AssociatedFile(
            filename="f.txt", storage_path=storage_path, size=10,
            sha256_hash="abc123", dataset_id=ds.id,
        )
        session.add(af)
        session.commit()
        session.refresh(af)
        return af.mfid


def test_gcs_backend_download_link_still_calls_generate_signed_url(client, db_engine, auth_as):
    file_mfid = _make_public_dataset_with_file(db_engine, "ds_pub_1")
    auth_as("0000-0000-0000-0001")

    with patch(
        "app.api.routes.files._generate_signed_url_link",
        return_value=("https://signed.example/x", 3600),
    ) as mock_sign:
        resp = client.get(f"/files/{file_mfid}/download_link")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": "https://signed.example/x", "expires_in": 3600}
    mock_sign.assert_called_once()


def test_associated_file_storage_backend_defaults_to_gcs(db_engine):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_model_1", resource_type="dataset", public=True))
        ds = Dataset(unique_id="ds_model_1", dataset_name="test", owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)

        af_default = AssociatedFile(filename="a.txt", size=1, sha256_hash="h1", dataset_id=ds.id)
        af_remote = AssociatedFile(
            filename="b.txt", size=2, sha256_hash="h2", dataset_id=ds.id,
            storage_backend="globus", storage_path="globus://endpoint/path/b.txt",
            access_note="request via NERSC allocation X",
        )
        session.add_all([af_default, af_remote])
        session.commit()
        session.refresh(af_default)
        session.refresh(af_remote)

        assert af_default.storage_backend == "gcs"
        assert af_default.access_note is None
        assert af_remote.storage_backend == "globus"
        assert af_remote.access_note == "request via NERSC allocation X"


def test_associated_file_create_schema_defaults_and_overrides():
    default = AssociatedFileCreate(filename="a.txt", size=1, sha256_hash="h1")
    assert default.storage_backend == "gcs"
    assert default.access_note is None

    remote = AssociatedFileCreate(
        filename="b.txt", size=2, sha256_hash="h2",
        storage_backend="lawrencium", access_note="ask PI for access",
    )
    assert remote.storage_backend == "lawrencium"
    assert remote.access_note == "ask PI for access"


def test_associated_file_read_and_update_schemas_have_new_fields():
    read = AssociatedFileRead(
        mfid="m1", dataset_id=1, dataset_mfid="ds1", filename="a.txt",
        storage_path=None, storage_backend="gcs", access_note=None,
        size=1, sha256_hash="h1",
    )
    assert read.storage_backend == "gcs"

    update = AssociatedFileUpdate()
    assert update.storage_backend is None
    assert update.access_note is None


def _grant_write_access(db_engine, dsid, user_id):
    with Session(db_engine) as session:
        session.add(Resource(mfid=dsid, resource_type="dataset", public=False))
        session.add(Dataset(unique_id=dsid, dataset_name="test", owner_orcid=user_id))
        ag = AccessGroup(group_name=dsid)
        session.add(ag)
        session.commit()
        session.refresh(ag)
        session.add(ResourceACL(mfid=dsid, accessgroup_id=ag.id, read=True, write=True))
        session.commit()
        return ag.id


def test_create_update_and_read_remote_file_round_trip(client, db_engine, auth_as):
    user_id = "0000-0000-0000-0001"
    ag_id = _grant_write_access(db_engine, "ds_roundtrip", user_id)
    auth_as(user_id, groups=[("ds_roundtrip", ag_id)])

    create_resp = client.post(
        "/datasets/ds_roundtrip/files",
        json={
            "filename": "remote.dat", "size": 42, "sha256_hash": "deadbeef",
            "storage_backend": "globus", "access_note": "ask PI for access",
        },
    )
    assert create_resp.status_code == 200, create_resp.text
    body = create_resp.json()
    assert body["storage_backend"] == "globus"
    assert body["access_note"] == "ask PI for access"
    file_mfid = body["mfid"]

    read_resp = client.get(f"/files/{file_mfid}")
    assert read_resp.status_code == 200, read_resp.text
    assert read_resp.json()["storage_backend"] == "globus"

    update_resp = client.patch(
        f"/files/{file_mfid}",
        json={"access_note": "updated: contact data steward"},
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["access_note"] == "updated: contact data steward"
    assert update_resp.json()["storage_backend"] == "globus"


def test_non_gcs_single_file_download_link_returns_null_without_calling_gcs(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_pub_2", resource_type="dataset", public=True))
        ds = Dataset(unique_id="ds_pub_2", dataset_name="test", public=True, owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)
        af = AssociatedFile(
            filename="remote.dat", size=1, sha256_hash="h1", dataset_id=ds.id,
            storage_backend="globus", storage_path="globus://endpoint/remote.dat",
        )
        session.add(af)
        session.commit()
        session.refresh(af)
        file_mfid = af.mfid

    auth_as("0000-0000-0000-0001")

    with patch("app.api.routes.files._generate_signed_url_link") as mock_sign, \
         patch("app.api.routes.files._ensure_storage_path") as mock_ensure:
        resp = client.get(f"/files/{file_mfid}/download_link")

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"url": None, "expires_in": None}
    mock_sign.assert_not_called()
    mock_ensure.assert_not_called()


def test_non_gcs_file_in_batch_download_links_is_null(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds_pub_3", resource_type="dataset", public=True))
        ds = Dataset(unique_id="ds_pub_3", dataset_name="test", public=True, owner_orcid="0000-0000-0000-0001")
        session.add(ds)
        session.commit()
        session.refresh(ds)
        gcs_af = AssociatedFile(
            filename="local.txt", storage_path="mf-storage-prod/ds_pub_3/local.txt",
            size=1, sha256_hash="h_gcs", dataset_id=ds.id,
        )
        remote_af = AssociatedFile(
            filename="remote.dat", storage_path="globus://endpoint/remote.dat",
            storage_backend="globus", size=1, sha256_hash="h_remote", dataset_id=ds.id,
        )
        session.add_all([gcs_af, remote_af])
        session.commit()
        session.refresh(gcs_af)
        session.refresh(remote_af)
        gcs_mfid, remote_mfid = gcs_af.mfid, remote_af.mfid

    auth_as("0000-0000-0000-0001")

    with patch(
        "app.api.routes.datasets._generate_signed_url_link",
        return_value=("https://signed.example/local.txt", 3600),
    ):
        resp = client.get("/datasets/ds_pub_3/download_links")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body[gcs_mfid] == "https://signed.example/local.txt"
    assert body[remote_mfid] is None
