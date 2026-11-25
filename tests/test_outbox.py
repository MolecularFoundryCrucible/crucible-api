import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlmodel import Session, select

from app.services import outbox
from app.tables import IngestionRequest, PostprocessingRun
from app.utils.permissions import Role
from tests.test_postprocessing import as_member, datasets  # noqa: F401
from tests.test_storage_download_routes import DS, _file, admin  # noqa: F401

LATER = timedelta(minutes=2)


def broker(fails=False):
    return patch("app.services.outbox.send_pika_message", side_effect=ConnectionError("broker down") if fails else None)


def rows(engine, model):
    with Session(engine) as session:
        return session.exec(select(model).order_by(model.id)).all()


def test_an_ingestion_request_the_broker_refuses_is_sent_later(client, db_engine, admin):  # noqa: F811
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    with broker(fails=True):
        response = client.post(f"/files/{file_mfid}/ingest")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["published_at"]) == ("requested", None)
    assert "queue_deferred" in [warning["code"] for warning in body["warnings"]]
    now = datetime.now(timezone.utc)
    with broker() as send:
        assert outbox.publish_pending(db_engine, now=now) == {"published": 0, "waiting": 0, "failed": 0}
        assert outbox.publish_pending(db_engine, now=now + LATER) == {"published": 1, "waiting": 0, "failed": 0}
        assert outbox.publish_pending(db_engine, now=now + 2 * LATER) == {"published": 0, "waiting": 0, "failed": 0}
    assert send.call_count == 1
    assert json.loads(send.call_args.kwargs["message"])["reqid"] == body["id"]
    [request] = rows(db_engine, IngestionRequest)
    assert (request.status, request.published_at is not None) == ("requested", True)


def test_a_sent_request_is_marked_published(client, db_engine, admin):  # noqa: F811
    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    with broker():
        response = client.post(f"/files/{file_mfid}/ingest")
    assert response.json()["published_at"] is not None
    assert "queue_deferred" not in [warning["code"] for warning in response.json()["warnings"]]


def test_a_postprocessing_run_the_broker_refuses_is_sent_later(client, datasets, auth_as):  # noqa: F811
    as_member(auth_as, Role.CONTRIBUTOR)
    with broker(fails=True):
        response = client.post("/datasets/ds1/postprocess", json={"pipeline": "carrier_segmentation"})
    assert response.status_code == 202, response.text
    assert (response.json()["status"], response.json()["published_at"]) == ("requested", None)
    with broker() as send:
        assert outbox.publish_pending(datasets, now=datetime.now(timezone.utc) + LATER)["published"] == 1
    assert json.loads(send.call_args.kwargs["message"])["run_id"] == response.json()["run_id"]


def test_work_that_cannot_be_sent_for_a_day_fails(client, datasets, auth_as):  # noqa: F811
    as_member(auth_as, Role.CONTRIBUTOR)
    with broker(fails=True):
        client.post("/datasets/ds1/postprocess", json={"pipeline": "carrier_segmentation"})
        assert outbox.publish_pending(datasets, now=datetime.now(timezone.utc) + LATER) == {"published": 0, "waiting": 1, "failed": 0}
        assert outbox.publish_pending(datasets, now=datetime.now(timezone.utc) + timedelta(days=2)) == {"published": 0, "waiting": 0, "failed": 1}
    [run] = rows(datasets, PostprocessingRun)
    assert (run.status, run.published_at, run.finished_at is not None) == ("failed", None, True)


def test_the_command_sends_pending_work_and_reports(client, db_engine, admin, monkeypatch, capsys):  # noqa: F811
    from app.cli.__main__ import main

    file_mfid = _file(db_engine, f"mf-storage-prod/{DS}/scan.pdf")
    with broker(fails=True):
        client.post(f"/files/{file_mfid}/ingest")
    monkeypatch.setattr("app.config.check_settings", lambda: None)
    monkeypatch.setattr(outbox, "GRACE", timedelta(0))
    with broker():
        assert main(["messaging", "publish-pending"]) == 0
    assert json.loads(capsys.readouterr().out) == {"published": 1, "waiting": 0, "failed": 0}
