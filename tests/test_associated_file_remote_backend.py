from unittest.mock import patch
from sqlmodel import Session
from app.models.core_models import Resource, Dataset, AssociatedFile
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
