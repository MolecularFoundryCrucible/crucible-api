from datetime import datetime, timezone

import pytest
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, AssociatedFile, Dataset, DatasetSampleLink, DeletionRequest, IngestionRequest, PostprocessingRun, Resource, ResourceACL, Sample, SampleLink, User, UserAccessGroupLink
from app.utils.permissions import Role

ME = "0000-0009-0000-0001"


@pytest.fixture
def records(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(User(first_name="P", last_name="D", email=None, unique_id=ME, username="pending"))
        session.add(AccessGroup(id=1, group_name="readers"))
        entities = [Dataset(unique_id=m, dataset_name=m) for m in ("ds", "ds-pending")] + [Sample(unique_id=m, sample_name=m) for m in ("sm", "sm-pending", "sm-parent")]
        session.add_all(entities)
        session.commit()
        for entity in entities:
            session.refresh(entity)
            session.add(Resource(mfid=entity.unique_id, resource_type="dataset" if isinstance(entity, Dataset) else "sample"))
            session.add(ResourceACL(mfid=entity.unique_id, accessgroup_id=1, read=True, write=False, permission=Role.VIEWER.db_value))
        by_mfid = {e.unique_id: e.id for e in entities}
        for sample in ("sm", "sm-pending"):
            session.add(SampleLink(parent_sample_id=by_mfid["sm-parent"], sample_id=by_mfid[sample]))
            for dataset in ("ds", "ds-pending"):
                session.add(DatasetSampleLink(sample_id=by_mfid[sample], dataset_id=by_mfid[dataset]))
        for dataset in ("ds", "ds-pending"):
            session.add(AssociatedFile(mfid=f"f-{dataset}", dataset_id=by_mfid[dataset], filename="a.h5", size=1, sha256_hash="a" * 64, storage_backend="globus", storage_path="globus://a.h5"))
            session.add(IngestionRequest(dataset_mfid=dataset, file_mfid=f"f-{dataset}", status="requested"))
            session.add(PostprocessingRun(dataset_mfid=dataset, pipeline="rga_analysis", requested_at=datetime.now(timezone.utc)))
        for mfid in ("ds-pending", "sm-pending"):
            session.add(DeletionRequest(resource_id=mfid, resource_type="dataset" if mfid.startswith("ds") else "sample", requester_id=ME, status="pending"))
        session.commit()
    auth_as(ME, groups=[("readers", 1, Role.VIEWER)])
    return client


@pytest.mark.parametrize("path, params", [
    ("/samples", {"dataset_mfid": "{ds}"}),
    ("/samples/facets", {"dataset_mfid": "{ds}", "field": "project"}),
    ("/datasets/facets", {"sample_mfid": "{sm}", "field": "project"}),
    ("/samples/{sm}/parents", {}),
    ("/samples/{sm}/children", {}),
    (f"/users/{ME}/datasets/{{ds}}", {}),
    ("/datasets", {"sample_mfid": "{sm}"}),
    ("/datasets/{ds}/parents", {}),
    ("/datasets/{ds}/children", {}),
    ("/datasets/{ds}/samples", {}),
    ("/ingestion_requests", {"dataset_mfid": "{ds}"}),
    ("/ingestion_requests", {"file_mfid": "f-{ds}"}),
    ("/postprocessing_runs", {"dataset_mfid": "{ds}"}),
])
def test_records_pending_deletion_are_not_found(records, path, params):
    def fill(value, suffix):
        return value.replace("{ds}", "ds" + suffix).replace("{sm}", "sm" + suffix)
    active = records.get(fill(path, ""), params={k: fill(v, "") for k, v in params.items()})
    assert active.status_code == 200, active.text
    pending = records.get(fill(path, "-pending"), params={k: fill(v, "-pending") for k, v in params.items()})
    assert pending.status_code == 404, pending.text


def _ids(session, model):
    return {row.dataset_mfid: row.id for row in session.exec(select(model)).all()}


@pytest.mark.parametrize("path", [
    "/datasets/ds-pending",
    "/datasets/ds-pending/files",
    "/files/f-ds-pending",
    "/files/f-ds-pending/download_link",
    "/ingestion_requests/{ingestion}",
    "/postprocessing_runs/{run}",
])
def test_exact_lookups_still_find_records_pending_deletion(records, db_engine, path):
    with Session(db_engine) as session:
        ingestion, run = _ids(session, IngestionRequest)["ds-pending"], _ids(session, PostprocessingRun)["ds-pending"]
    response = records.get(path.format(ingestion=ingestion, run=run))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("path, key", [
    ("/files", "mfid"),
    ("/ingestion_requests", "dataset_mfid"),
    ("/postprocessing_runs", "dataset_mfid"),
])
def test_lists_hide_records_pending_deletion(records, path, key):
    response = records.get(path)
    assert response.status_code == 200, response.text
    assert sorted(item[key] for item in response.json()["items"]) == (["f-ds"] if key == "mfid" else ["ds"])


@pytest.fixture
def writer(records, db_engine, auth_as, monkeypatch):
    monkeypatch.setattr("app.api.routes.gcs_storage.engine", db_engine)
    monkeypatch.setattr("app.api.routes.multipart.engine", db_engine)
    monkeypatch.setenv("MULTIPART_UPLOAD_ENABLED", "true")
    monkeypatch.setenv("UPLOAD_BUCKET", "dedicated")
    monkeypatch.setenv("MULTIPART_UPLOAD_BUCKET", "dedicated")
    monkeypatch.setenv("MULTIPART_UPLOAD_SIGNER", "signer@example.iam.gserviceaccount.com")
    monkeypatch.setenv("MULTIPART_UPLOAD_PROJECT", "test-project")
    with Session(db_engine) as session:
        for acl in session.exec(select(ResourceACL)).all():
            acl.permission = Role.ADMIN.db_value
            acl.write = True
            session.add(acl)
        me = session.exec(select(User).where(User.unique_id == ME)).one()
        session.add(UserAccessGroupLink(user_id=me.id, accessgroup_id=1, role="owner"))
        session.commit()
    auth_as(ME, groups=[("readers", 1, Role.OWNER)])
    return records


@pytest.mark.parametrize("method, path, body", [
    ("post", "/datasets/ds-pending/files", {"filename": "b.h5", "size": 1, "sha256_hash": "b" * 64}),
    ("patch", "/files/f-ds-pending", {"access_note": "x"}),
    ("delete", "/files/f-ds-pending", None),
    ("post", "/files/f-ds-pending/ingest", None),
    ("post", "/datasets/ds-pending/postprocess", {"pipeline": "rga_analysis"}),
    ("post", "/datasets/ds-pending/upload/initiate", {"filename": "b.h5", "size": 1}),
    ("post", "/datasets/ds-pending/upload/multipart/initiate", {"filename": "b.h5", "size": 1}),
    ("post", "/datasets/ds/children/ds-pending", None),
    ("post", "/datasets/ds-pending/samples/sm", None),
    ("post", "/samples/sm-parent/children/sm-pending", None),
])
def test_writes_to_records_pending_deletion_conflict(writer, method, path, body):
    response = writer.request(method, path, json=body)
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "pending_deletion"


def test_consumers_can_finish_work_on_records_pending_deletion(writer, db_engine):
    with Session(db_engine) as session:
        ingestion, run = _ids(session, IngestionRequest)["ds-pending"], _ids(session, PostprocessingRun)["ds-pending"]
    assert writer.patch(f"/ingestion_requests/{ingestion}", json={"status": "complete"}).status_code == 200
    assert writer.patch(f"/postprocessing_runs/{run}", json={"status": "completed"}).status_code == 200


def test_unreadable_and_missing_files_look_the_same(records, db_engine, auth_as):
    auth_as("0000-0009-0000-0002", groups=[])
    unreadable, missing = records.get("/files/f-ds"), records.get("/files/no-such-file")
    assert (unreadable.status_code, unreadable.json()) == (missing.status_code, missing.json()) == (404, {"detail": "File not found"})
