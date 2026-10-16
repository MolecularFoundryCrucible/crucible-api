import json
from unittest.mock import patch

import pytest
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Dataset, DeletionRequest, PostprocessingRun, Resource, ResourceACL, User
from app.utils.permissions import Role

ME = "0000-0005-0000-0001"
STRANGER = "0000-0005-0000-0002"


@pytest.fixture
def datasets(db_engine):
    with Session(db_engine) as session:
        session.add(AccessGroup(id=1, group_name="proj"))
        for orcid, name in ((ME, "me"), (STRANGER, "stranger")):
            session.add(User(first_name=name, last_name=name, email=None, unique_id=orcid, username=name))
        for mfid in ("ds1", "ds2", "doomed"):
            session.add(Resource(mfid=mfid, resource_type="dataset"))
            session.add(Dataset(unique_id=mfid, dataset_name=mfid))
            session.add(ResourceACL(mfid=mfid, accessgroup_id=1, read=True, write=True, permission=Role.ADMIN.db_value))
        session.add(DeletionRequest(resource_id="doomed", resource_type="dataset", requester_id=ME, status="pending"))
        session.commit()
    return db_engine


@pytest.fixture
def queue():
    with patch("app.api.routes.pipelines.send_pika_message") as send, patch("app.api.routes.pipelines.get_secret", return_value="pw"):
        yield send


def runs(engine):
    with Session(engine) as session:
        return session.exec(select(PostprocessingRun).order_by(PostprocessingRun.id)).all()


def as_member(auth_as, role, user=ME):
    auth_as(user, groups=[("proj", 1, role)])


def test_lists_pipelines(client, auth_as):
    auth_as(STRANGER, groups=[])
    pipelines = {pipeline["name"]: pipeline for pipeline in client.get("/pipelines").json()}
    assert set(pipelines) == {"carrier_segmentation", "insitu_aggregation", "rga_analysis", "mosaic_stitch"}
    assert pipelines["rga_analysis"]["parameters_schema"]["additionalProperties"] is False


def test_contributor_queues_a_run(client, datasets, auth_as, queue):
    as_member(auth_as, Role.CONTRIBUTOR)
    response = client.post("/datasets/ds1/postprocess", json={"pipeline": "carrier_segmentation"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body | {"requested_at": None, "run_id": None} == {
        "run_id": None, "dataset_mfid": "ds1", "pipeline": "carrier_segmentation", "parameters": {}, "status": "requested",
        "error": None, "requested_by": ME, "requested_at": None, "started_at": None, "finished_at": None,
    }
    queue.assert_called_once()
    assert queue.call_args.kwargs["routing_key"] == "carrier-segmentation"
    assert json.loads(queue.call_args.kwargs["message"]) == {
        "run_id": body["run_id"], "dataset_mfid": "ds1", "dsid": "ds1", "pipeline": "carrier_segmentation", "parameters": {}, "requested_by": ME,
    }


def test_repeat_requests_start_new_runs(client, datasets, auth_as, queue):
    as_member(auth_as, Role.CONTRIBUTOR)
    for _ in range(2):
        assert client.post("/datasets/ds1/postprocess", json={"pipeline": "rga_analysis"}).status_code == 202
    assert len(runs(datasets)) == 2


@pytest.mark.parametrize("dataset, role, user, status", [
    ("ds1", Role.VIEWER, ME, 403),
    ("ds1", None, STRANGER, 404),
    ("missing", Role.OWNER, ME, 404),
    ("doomed", Role.OWNER, ME, 404),
])
def test_refused_requests_queue_nothing(client, datasets, auth_as, queue, dataset, role, user, status):
    if role is None:
        auth_as(user, groups=[])
    else:
        as_member(auth_as, role, user)
    assert client.post(f"/datasets/{dataset}/postprocess", json={"pipeline": "mosaic_stitch"}).status_code == status
    queue.assert_not_called()
    assert runs(datasets) == []


@pytest.mark.parametrize("body, code", [
    ({"pipeline": "nope"}, "unknown_pipeline"),
    ({"pipeline": "mosaic_stitch", "parameters": {"tiles": 4}}, "invalid_parameters"),
])
def test_bad_requests_are_422(client, datasets, auth_as, queue, body, code):
    as_member(auth_as, Role.CONTRIBUTOR)
    response = client.post("/datasets/ds1/postprocess", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == code
    queue.assert_not_called()


def test_publish_failure_records_failed_run(client, datasets, auth_as, queue):
    queue.side_effect = ConnectionError("broker down")
    as_member(auth_as, Role.CONTRIBUTOR)
    response = client.post("/datasets/ds1/postprocess", json={"pipeline": "carrier_segmentation"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "pipeline_unavailable"
    [run] = runs(datasets)
    assert (run.status, run.error, run.finished_at is not None) == ("failed", "Could not queue the run", True)


@pytest.mark.parametrize("path, pipeline", [
    ("carrier_segmentation", "carrier_segmentation"),
    ("insitu_spec_aggregation", "insitu_aggregation"),
    ("rga_analysis", "rga_analysis"),
    ("mosaic_stitch", "mosaic_stitch"),
])
def test_deprecated_routes_start_runs_with_the_same_checks(client, datasets, auth_as, queue, path, pipeline):
    as_member(auth_as, Role.VIEWER)
    assert client.post(f"/datasets/ds1/{path}").status_code == 403
    as_member(auth_as, Role.CONTRIBUTOR)
    response = client.post(f"/datasets/ds1/{path}")
    assert response.status_code == 200, response.text
    assert response.json()["pipeline"] == pipeline
    assert [run.pipeline for run in runs(datasets)] == [pipeline]


def test_list_and_read_runs_follow_dataset_access(client, datasets, auth_as, queue):
    as_member(auth_as, Role.CONTRIBUTOR)
    first = client.post("/datasets/ds1/postprocess", json={"pipeline": "rga_analysis"}).json()["run_id"]
    client.post("/datasets/ds2/postprocess", json={"pipeline": "mosaic_stitch"})

    listed = client.get("/postprocessing_runs").json()
    assert listed["total"] == 2
    assert [run["dataset_mfid"] for run in listed["items"]] == ["ds2", "ds1"]
    assert [run["run_id"] for run in client.get("/postprocessing_runs", params={"dataset_mfid": "ds1"}).json()["items"]] == [first]
    assert client.get("/postprocessing_runs", params={"pipeline": "mosaic_stitch"}).json()["total"] == 1
    assert client.get(f"/postprocessing_runs/{first}").json()["pipeline"] == "rga_analysis"

    auth_as(STRANGER, groups=[])
    assert client.get("/postprocessing_runs").json()["total"] == 0
    assert client.get(f"/postprocessing_runs/{first}").status_code == 404
    assert client.get("/postprocessing_runs", params={"dataset_mfid": "ds1"}).status_code == 404


def test_status_updates(client, datasets, auth_as, queue):
    as_member(auth_as, Role.CONTRIBUTOR)
    run_id = client.post("/datasets/ds1/postprocess", json={"pipeline": "rga_analysis"}).json()["run_id"]
    assert client.patch(f"/postprocessing_runs/{run_id}", json={"status": "running"}).status_code == 403

    as_member(auth_as, Role.EDITOR)
    running = client.patch(f"/postprocessing_runs/{run_id}", json={"status": "running"}).json()
    assert running["status"] == "running" and running["started_at"] is not None and running["finished_at"] is None
    failed = client.patch(f"/postprocessing_runs/{run_id}", json={"status": "failed", "error": "no RGA channels"}).json()
    assert (failed["status"], failed["error"], failed["started_at"]) == ("failed", "no RGA channels", running["started_at"])
    assert failed["finished_at"] is not None

    again = client.patch(f"/postprocessing_runs/{run_id}", json={"status": "completed"})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "run_finished"
    assert client.patch(f"/postprocessing_runs/{run_id}", json={"status": "requested"}).status_code == 422
