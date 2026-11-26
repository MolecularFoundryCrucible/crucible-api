"""Ingestion requests and postprocessing runs are saved first and sent to RabbitMQ after, so a broker
that is down, or a process that stops between the two, delays the work instead of losing it:
published_at stays empty until the broker confirms the message, and publish_pending sends what is
still unsent."""
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.config import get_secret, get_settings
from app.services.pipelines import PIPELINES
from app.tables import AssociatedFile, IngestionRequest, PostprocessingRun
from app.utils.rmq_utils import get_ingestion_tier, send_pika_message

logger = logging.getLogger(__name__)

GRACE = timedelta(seconds=60)
GIVE_UP = timedelta(days=1)
UNSENT = "Could not be sent to the queue"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _ingestion_message(session: Session, request: IngestionRequest):
    file = session.exec(select(AssociatedFile).where(AssociatedFile.mfid == request.file_mfid)).one_or_none() if request.file_mfid else None
    if file is None or not file.storage_path:
        return None
    body = {
        "filename": file.storage_path,
        "store_id": file.store_id,
        **({"storage_generation": file.storage_version} if file.storage_version else {}),
        "file_name": file.filename,
        "reqid": request.id,
        "dsid": request.dataset_mfid,
        "ingestion_class": request.ingestion_class,
    }
    return f"ingestion-{get_settings().ingestion.routing_suffix}-{get_ingestion_tier(file.size or 0)}", json.dumps(body)


def _run_message(session: Session, run: PostprocessingRun):
    pipeline = PIPELINES.get(run.pipeline)
    if pipeline is None:
        return None
    body = {"run_id": run.id, "dataset_mfid": run.dataset_mfid, "dsid": run.dataset_mfid, "pipeline": run.pipeline, "parameters": run.parameters, "requested_by": run.requested_by}
    return pipeline.routing_key, json.dumps(body)


MESSAGES = {IngestionRequest: _ingestion_message, PostprocessingRun: _run_message}


def _fail(session: Session, row) -> None:
    row.status = "failed"
    if isinstance(row, PostprocessingRun):
        row.error, row.finished_at = UNSENT, utcnow()
    session.add(row)
    session.commit()


def publish(engine, model, row_id: int) -> bool:
    """Send one unsent request or run; True once the broker has confirmed it (or it needs no sending).
    Work whose message cannot be built any more, such as a deleted file, fails."""
    with Session(engine) as session:
        row = session.get(model, row_id)
        if row is None or row.published_at is not None or row.status != "requested":
            return True
        message = MESSAGES[model](session, row)
        if message is None:
            _fail(session, row)
            return True
    routing_key, body = message
    try:
        send_pika_message(message=body, exchange="", routing_key=routing_key, rmq_pw=get_secret("RABBITMQ_DEFAULT_PW"))
    except Exception as error:
        logger.warning("queue_publish_deferred %s=%s routing_key=%s error=%s", model.__name__, row_id, routing_key, type(error).__name__)
        return False
    with Session(engine) as session:
        row = session.get(model, row_id)
        if row is not None and row.published_at is None:
            row.published_at = utcnow()
            session.add(row)
            session.commit()
    logger.info("queue_published %s=%s routing_key=%s", model.__name__, row_id, routing_key)
    return True


def _created(row) -> datetime | None:
    if isinstance(row, PostprocessingRun):
        return _utc(row.requested_at)
    try:
        return _utc(datetime.fromisoformat(row.time_created)) if row.time_created else None
    except ValueError:
        return None


def publish_pending(engine, now: datetime | None = None) -> dict[str, int]:
    """Send every request and run still unsent GRACE after it was made, so the route that made it has
    had its own try; one still unsent GIVE_UP after it was made fails. Run it every minute, one at a time."""
    current = now or utcnow()
    counts = {"published": 0, "waiting": 0, "failed": 0}
    if not get_settings().ingestion.enabled:
        return counts
    for model in (IngestionRequest, PostprocessingRun):
        with Session(engine) as session:
            pending = [(row.id, _created(row)) for row in session.exec(select(model).where(model.status == "requested", model.published_at.is_(None)).order_by(model.id)).all()]
        for row_id, created in pending:
            if created is not None and current - created < GRACE:
                continue
            if publish(engine, model, row_id):
                with Session(engine) as session:
                    row = session.get(model, row_id)
                    counts["failed" if row is not None and row.status == "failed" else "published"] += 1
            elif created is None or current - created > GIVE_UP:
                with Session(engine) as session:
                    _fail(session, session.get(model, row_id))
                counts["failed"] += 1
                logger.warning("queue_publish_given_up %s=%s", model.__name__, row_id)
            else:
                counts["waiting"] += 1
    return counts
