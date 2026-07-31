from unittest.mock import patch
from sqlmodel import Session
from app.models.core_models import Resource, Dataset, AssociatedFile


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
