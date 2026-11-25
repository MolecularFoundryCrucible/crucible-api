import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from google.api_core.exceptions import NotFound
from sqlmodel import Session, select

import app.api.routes.gcs_storage as gcs_storage
import app.storage.gcs as google
from app.tables import AssociatedFile, Dataset, IngestionRequest, Resource
from app.utils.permissions import PlatformRole
from tests.stores import gcs, no_stores, s3, use_stores

DS = "0t3qajrnzhtn7000x6s0x427hg"
OTHER = "0t3qajvcgnvx7000h49ya23r7c"


def _file(db_engine, storage_path, filename="scan.pdf", generation=None, backend="gcs", dsid=DS, store_id="mf-storage-prod"):
    with Session(db_engine) as session:
        dataset = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if dataset is None:
            session.add(Resource(mfid=dsid, resource_type="dataset"))
            dataset = Dataset(unique_id=dsid, dataset_name="test", owner_orcid="0000-0000-0000-0001")
            session.add(dataset)
            session.commit()
            session.refresh(dataset)
        af = AssociatedFile(filename=filename, storage_path=storage_path, storage_backend=backend, storage_generation=generation, size=10, sha256_hash="a" * 64, dataset_id=dataset.id, store_id=store_id if backend == "gcs" else None)
        session.add(af)
        session.commit()
        session.refresh(af)
        return af.mfid


@pytest.fixture
def signer(monkeypatch):
    client = MagicMock()
    blob = client.bucket.return_value.blob.return_value
    blob.content_type = "application/octet-stream"
    blob.generate_signed_url.return_value = "https://signed.example/x"
    monkeypatch.setattr(google, "server_client", lambda: client)
    monkeypatch.setattr(gcs_storage, "get_store", lambda *_: google.GoogleStore(SimpleNamespace(bucket=None, signer_service_account=None, project=None)))
    return client


@pytest.fixture
def admin(auth_as):
    auth_as("0000-0000-0000-0009", platform_role=PlatformRole.ADMIN)


def test_own_dataset_file_downloads_with_record_filename(client, db_engine, signer, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf", filename="results/scan.pdf")
    response = client.get(f"/files/{file_mfid}/download_link")
    assert response.status_code == 200, response.text
    assert response.json() == {"url": "https://signed.example/x", "expires_in": 3600}
    kwargs = signer.bucket.return_value.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"].startswith("attachment; filename=\"scan.pdf\"")
    assert "response_type" not in kwargs


def test_download_link_defaults_to_attachment_without_type_override(client, db_engine, signer, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    response = client.get(f"/files/{file_mfid}/download_link")
    assert response.status_code == 200, response.text
    kwargs = signer.bucket.return_value.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"].startswith("attachment;")
    assert "response_type" not in kwargs


def test_download_link_inline_guesses_type_for_pdf(client, db_engine, signer, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    response = client.get(f"/files/{file_mfid}/download_link?disposition=inline")
    assert response.status_code == 200, response.text
    kwargs = signer.bucket.return_value.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"].startswith("inline;")
    assert kwargs["response_type"] == "application/pdf"


def test_download_link_inline_still_forces_attachment_for_html(client, db_engine, signer, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/page.html", filename="page.html")
    response = client.get(f"/files/{file_mfid}/download_link?disposition=inline")
    assert response.status_code == 200, response.text
    kwargs = signer.bucket.return_value.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"].startswith("attachment;")


def test_download_link_rejects_an_unknown_disposition(client, db_engine, signer, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    response = client.get(f"/files/{file_mfid}/download_link?disposition=bogus")
    assert response.status_code == 422


def test_bulk_links_honor_inline_disposition(client, db_engine, signer, admin):
    _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    response = client.get(f"/datasets/{DS}/download_links?disposition=inline")
    assert response.status_code == 200, response.text
    kwargs = signer.bucket.return_value.blob.return_value.generate_signed_url.call_args.kwargs
    assert kwargs["response_disposition"].startswith("inline;")


def test_bulk_links_reject_an_unknown_disposition(client, db_engine, signer, admin):
    _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    response = client.get(f"/datasets/{DS}/download_links?disposition=bogus")
    assert response.status_code == 422


def test_file_pointing_at_another_dataset_is_refused(client, db_engine, signer, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{OTHER}/secret.pdf")
    response = client.get(f"/files/{file_mfid}/download_link")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "storage_location_invalid", "reason": "outside_dataset", "message": "File storage location is not valid: outside_dataset"}
    signer.bucket.assert_not_called()


def test_file_pointing_at_another_bucket_is_refused(client, db_engine, signer, admin):
    file_mfid = _file(db_engine, f"keys-and-certs/{DS}/key.json")
    response = client.get(f"/files/{file_mfid}/download_link")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "storage_location_invalid", "reason": "bucket_not_allowed", "message": "File storage location is not valid: bucket_not_allowed"}


def test_missing_object_returns_404(client, db_engine, signer, admin):
    signer.bucket.return_value.blob.return_value.reload.side_effect = NotFound("gone")
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    assert client.get(f"/files/{file_mfid}/download_link").status_code == 404


def test_bulk_links_null_only_the_refused_file(client, db_engine, signer, admin):
    good = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    bad = _file(db_engine, f"mf-storage-prod/{OTHER}/secret.pdf", filename="secret.pdf")
    response = client.get(f"/datasets/{DS}/download_links")
    assert response.status_code == 200, response.text
    assert response.json() == {good: "https://signed.example/x", bad: None}


def test_registered_gcs_record_without_path_downloads_via_backfill(client, db_engine, signer, admin, monkeypatch):
    _file(db_engine, f"mf-storage-prod/{DS}/existing.pdf", filename="existing.pdf")
    created = client.post(f"/datasets/{DS}/files", json={"filename": "placed.pdf", "size": 5, "sha256_hash": "d" * 64})
    assert created.status_code == 200, created.text
    assert created.json()["storage_path"] is None
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", lambda dsid, filename, store_id: f"mf-storage-prod/{dsid}/{filename}")
    response = client.get(f"/files/{created.json()['mfid']}/download_link")
    assert response.status_code == 200, response.text
    signer.bucket.return_value.blob.assert_called_with(f"{DS}/placed.pdf", generation=None)


def test_backfill_skips_multipart_objects(db_engine, monkeypatch):
    listed = [SimpleNamespace(name=f"{DS}/_files/abc-scan.pdf"), SimpleNamespace(name=f"{DS}/raw/scan.pdf")]
    client = MagicMock()
    client.bucket.return_value.list_blobs.return_value = listed
    monkeypatch.setattr(gcs_storage, "store_client", lambda store: client)
    assert gcs_storage._determine_storage_path(DS, "scan.pdf", "mf-storage-prod") == f"mf-storage-prod/{DS}/raw/scan.pdf"


def _listing(monkeypatch, *names):
    client = MagicMock()
    client.bucket.return_value.list_blobs.return_value = [SimpleNamespace(name=f"{DS}/{name}") for name in names]
    monkeypatch.setattr(gcs_storage, "store_client", lambda store: client)


def test_backfill_needs_a_whole_name_match(monkeypatch):
    _listing(monkeypatch, "foobar.tif", "raw/bar.tif")
    assert gcs_storage._determine_storage_path(DS, "bar.tif", "mf-storage-prod") == f"mf-storage-prod/{DS}/raw/bar.tif"
    _listing(monkeypatch, "foobar.tif")
    with pytest.raises(HTTPException) as error:
        gcs_storage._determine_storage_path(DS, "bar.tif", "mf-storage-prod")
    assert error.value.status_code == 404


def test_backfill_prefers_the_exact_path_and_refuses_to_guess(monkeypatch):
    _listing(monkeypatch, "a/bar.tif", "bar.tif", "b/bar.tif")
    assert gcs_storage._determine_storage_path(DS, "bar.tif", "mf-storage-prod") == f"mf-storage-prod/{DS}/bar.tif"
    _listing(monkeypatch, "a/bar.tif", "b/bar.tif")
    with pytest.raises(HTTPException) as error:
        gcs_storage._determine_storage_path(DS, "bar.tif", "mf-storage-prod")
    assert error.value.status_code == 409


def _ingestion_requests(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(IngestionRequest)).all()


def test_ingest_refuses_a_file_outside_its_dataset(client, db_engine, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{OTHER}/secret.pdf")
    with patch("app.services.outbox.send_pika_message") as send, patch("app.services.outbox.get_secret", return_value="pw"):
        response = client.post(f"/files/{file_mfid}/ingest")
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "storage_location_invalid", "reason": "outside_dataset", "message": "File storage location is not valid: outside_dataset"}
    send.assert_not_called()
    assert _ingestion_requests(db_engine) == []


def test_ingest_backfills_missing_storage_path(client, db_engine, admin, monkeypatch):
    file_mfid = _file(db_engine, None)
    monkeypatch.setattr(gcs_storage, "_determine_storage_path", lambda dsid, filename, store_id: f"mf-storage-prod/{dsid}/{filename}")
    with patch("app.services.outbox.send_pika_message") as send, patch("app.services.outbox.get_secret", return_value="pw"):
        response = client.post(f"/files/{file_mfid}/ingest")
    assert response.status_code == 200, response.text
    assert response.json()["file_to_upload"] == f"mf-storage-prod/{DS}/scan.pdf"
    assert f"mf-storage-prod/{DS}/scan.pdf" in send.call_args.kwargs["message"]


PATH_ERROR = "storage_path is assigned by the upload API for GCS files"
CONTENT_ERROR = "size and sha256_hash are determined by the stored object for GCS files"


@pytest.mark.parametrize("body, detail", [
    ({"storage_path": f"mf-storage-prod/{OTHER}/secret.pdf"}, PATH_ERROR),
    ({"size": 11}, CONTENT_ERROR),
    ({"sha256_hash": "b" * 64}, CONTENT_ERROR),
])
def test_patch_rejects_gcs_content_fields(client, db_engine, admin, body, detail):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    response = client.patch(f"/files/{file_mfid}", json=body)
    assert response.status_code == 422
    assert response.json()["detail"] == detail


def test_patch_cannot_switch_a_located_record_to_gcs(client, db_engine, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{OTHER}/secret.pdf", backend="globus")
    response = client.patch(f"/files/{file_mfid}", json={"storage_backend": "gcs"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "storage_backend_class_change"


def test_patch_echoing_current_values_is_accepted(client, db_engine, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    body = {"filename": "renamed.pdf", "storage_path": f"mf-storage-prod/{DS}/scan.pdf", "storage_backend": "gcs", "size": 10, "sha256_hash": "a" * 64, "access_note": "note"}
    response = client.patch(f"/files/{file_mfid}", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == "renamed.pdf"


def test_patch_may_clear_a_gcs_path(client, db_engine, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    response = client.patch(f"/files/{file_mfid}", json={"storage_path": None})
    assert response.status_code == 200, response.text
    assert response.json()["storage_path"] is None


def test_patch_keeps_free_form_edits_for_catalog_records(client, db_engine, admin):
    file_mfid = _file(db_engine, "globus://endpoint/a.dat", backend="globus")
    response = client.patch(f"/files/{file_mfid}", json={"storage_path": "globus://endpoint/b.dat", "size": 99, "sha256_hash": "c" * 64})
    assert response.status_code == 200, response.text
    assert response.json()["storage_path"] == "globus://endpoint/b.dat"


MULTIPART = "mf-crucible-multipart-staging"


def test_patch_cannot_clear_the_path_of_a_published_multipart_record(client, db_engine, admin, monkeypatch):
    use_stores(monkeypatch, gcs(read_buckets=[MULTIPART]))
    file_mfid = _file(db_engine, f"{MULTIPART}/{DS}/_files/abc", generation="7")
    response = client.patch(f"/files/{file_mfid}", json={"storage_path": None})
    assert response.status_code == 422
    assert response.json()["detail"] == PATH_ERROR


def test_patch_cannot_switch_a_published_multipart_record_off_gcs(client, db_engine, admin, monkeypatch):
    use_stores(monkeypatch, gcs(read_buckets=[MULTIPART]))
    file_mfid = _file(db_engine, f"{MULTIPART}/{DS}/_files/abc", generation="7")
    response = client.patch(f"/files/{file_mfid}", json={"storage_backend": "local"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "storage_backend_class_change"


def test_patch_may_rename_a_published_multipart_record(client, db_engine, admin, monkeypatch):
    use_stores(monkeypatch, gcs(read_buckets=[MULTIPART]))
    file_mfid = _file(db_engine, f"{MULTIPART}/{DS}/_files/abc", generation="7")
    response = client.patch(f"/files/{file_mfid}", json={"filename": "renamed.pdf"})
    assert response.status_code == 200, response.text
    assert response.json()["filename"] == "renamed.pdf"
    with Session(db_engine) as session:
        af = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == file_mfid)).one()
        assert af.storage_generation == "7"


def test_legacy_gcs_record_cannot_switch_to_a_non_gcs_backend(client, db_engine, admin):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    switched = client.patch(f"/files/{file_mfid}", json={"storage_backend": "local"})
    assert switched.status_code == 409
    assert switched.json()["detail"]["code"] == "storage_backend_class_change"
    assert client.get(f"/files/{file_mfid}").json()["storage_backend"] == "gcs"


def test_catalog_only_record_lifecycle_is_unchanged(client, db_engine, admin):
    _file(db_engine, f"mf-storage-prod/{DS}/existing.pdf")
    created = client.post(f"/datasets/{DS}/files", json={"filename": "remote.h5", "size": 42, "sha256_hash": "e" * 64, "storage_backend": "globus", "storage_path": "globus://endpoint/a/remote.h5", "access_note": "on NERSC"})
    assert created.status_code == 200, created.text
    file_mfid = created.json()["mfid"]
    again = client.post(f"/datasets/{DS}/files", json={"filename": "remote.h5", "size": 43, "sha256_hash": "f" * 64, "storage_backend": "globus"})
    assert again.status_code == 409
    assert again.json()["detail"]["file_mfid"] == file_mfid
    updated = client.patch(f"/files/{file_mfid}", json={"storage_path": "/global/cfs/b/remote.h5", "storage_backend": "local", "size": 43, "sha256_hash": "f" * 64})
    assert updated.status_code == 200, updated.text
    assert updated.json()["storage_path"] == "/global/cfs/b/remote.h5"
    assert client.get(f"/files/{file_mfid}/download_link").json() == {"url": None, "expires_in": None}
    assert client.get(f"/datasets/{DS}/download_links").json()[file_mfid] is None
    assert client.post(f"/files/{file_mfid}/ingest").status_code == 400


def _switch(monkeypatch, name, value):
    from app.config import get_settings

    if value is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


def test_ingest_disabled_creates_no_request(client, db_engine, admin, monkeypatch):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    _switch(monkeypatch, "INGESTION_ENABLED", None)
    no_stores(monkeypatch)
    with patch("app.services.outbox.send_pika_message") as send:
        response = client.post(f"/files/{file_mfid}/ingest")
    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "feature_disabled"
    assert response.json()["detail"]["feature"] == "ingestion"
    send.assert_not_called()
    assert _ingestion_requests(db_engine) == []


def test_ingest_not_requested_records_without_ingestion(client, db_engine, admin, monkeypatch):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    _switch(monkeypatch, "INGESTION_ENABLED", "false")
    with patch("app.services.outbox.send_pika_message") as send:
        response = client.post(f"/files/{file_mfid}/ingest", params={"status": "not_requested"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "not_requested"
    send.assert_not_called()



def test_ingest_publishes_with_configured_routing(client, db_engine, admin, monkeypatch):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    _switch(monkeypatch, "RMQ_ROUTING_SUFFIX", "lab")
    with patch("app.services.outbox.send_pika_message") as send:
        response = client.post(f"/files/{file_mfid}/ingest")
    assert response.status_code == 200, response.text
    assert send.call_args.kwargs["routing_key"] == "ingestion-lab-small"
    assert "user" not in send.call_args.kwargs
    assert json.loads(send.call_args.kwargs["message"])["store_id"] == "mf-storage-prod"


def test_download_without_storage_is_not_configured(client, db_engine, signer, admin, monkeypatch):
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    no_stores(monkeypatch)
    response = client.get(f"/files/{file_mfid}/download_link")
    assert response.status_code == 501
    assert response.json()["detail"]["code"] == "storage_not_configured"
    signer.bucket.assert_not_called()


def test_a_file_without_a_location_is_not_found_on_s3(client, db_engine, admin, monkeypatch):
    def refuse(store):
        raise AssertionError("Google storage was asked")
    monkeypatch.setattr(gcs_storage, "store_client", refuse)
    use_stores(monkeypatch, s3("mf-storage-prod", "mf-storage-prod"))
    file_mfid = _file(db_engine, None)
    assert client.get(f"/files/{file_mfid}/download_link").status_code == 404
    response = client.get(f"/datasets/{DS}/download_links")
    assert response.status_code == 200, response.text
    assert response.json() == {file_mfid: None}
