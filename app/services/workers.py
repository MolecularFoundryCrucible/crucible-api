from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import get_settings
from app.models.ingestion import IngestWarning
from app.models.workers import WorkerRead, WorkerRegister, WorkerRegistered
from app.tables import Worker

TTL = timedelta(minutes=10)
HEARTBEAT = timedelta(minutes=2, seconds=30)
RETAIN = timedelta(days=1)


def now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _live(row: Worker) -> bool:
    return _utc(row.last_seen_at) > now() - TTL


def _read(row: Worker) -> WorkerRead:
    stores = sorted(store.id for store in get_settings().storage.stores if store.bucket in row.buckets)
    return WorkerRead(worker_id=row.worker_id, kind=row.kind, capabilities=row.capabilities, buckets=row.buckets, store_ids=stores, worker_version=row.worker_version, package_version=row.package_version, registered_at=row.registered_at, last_seen_at=row.last_seen_at, expires_at=_utc(row.last_seen_at) + TTL, live=_live(row))


def register(engine, principal, worker_id: str, body: WorkerRegister) -> WorkerRegistered:
    """Create or renew a registration. A live worker id belongs to the account that registered it;
    an expired one may be taken over. Registrations unseen for RETAIN are removed."""
    for attempt in range(2):
        with Session(engine) as session:
            current = now()
            session.execute(delete(Worker).where(Worker.last_seen_at < current - RETAIN, Worker.worker_id != worker_id))
            row = session.exec(select(Worker).where(Worker.worker_id == worker_id).with_for_update()).one_or_none()
            if row is not None and row.registered_by != principal.user_id:
                if _live(row):
                    raise HTTPException(409, {"code": "worker_id_taken", "worker_id": worker_id, "message": "Another account's live worker has this id"})
                session.delete(row)
                session.flush()
                row = None
            if row is None:
                row = Worker(worker_id=worker_id, kind=body.kind, registered_by=principal.user_id, registered_at=current, last_seen_at=current)
            row.kind, row.capabilities, row.buckets = body.kind, list(body.capabilities), list(body.buckets)
            row.worker_version, row.package_version, row.last_seen_at = body.worker_version, body.package_version, current
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                if attempt:
                    raise
                continue
            session.refresh(row)
            return WorkerRegistered(**_read(row).model_dump(), heartbeat_seconds=int(HEARTBEAT.total_seconds()))


def deregister(engine, worker_id: str) -> None:
    with Session(engine) as session:
        session.execute(delete(Worker).where(Worker.worker_id == worker_id))
        session.commit()


def _live_rows(session: Session, kind: str) -> list[Worker]:
    return list(session.exec(select(Worker).where(Worker.kind == kind, Worker.last_seen_at > now() - TTL)).all())


def capabilities(session: Session, kind: str) -> list[str]:
    """The sorted union of what live workers of this kind can do."""
    return sorted({name for row in _live_rows(session, kind) for name in row.capabilities})


def list_workers(session: Session, kind: str | None = None, live: bool | None = None) -> list[WorkerRead]:
    query = select(Worker).order_by(Worker.worker_id)
    if kind is not None:
        query = query.where(Worker.kind == kind)
    if live is not None:
        cutoff = now() - TTL
        query = query.where(Worker.last_seen_at > cutoff if live else Worker.last_seen_at <= cutoff)
    return [_read(row) for row in session.exec(query).all()]


def ingest_warnings(session: Session, ingestion_class: str | None, bucket: str | None) -> list[IngestWarning]:
    """Why a queued ingestion request may wait: no live worker offers its ingestor, or none reads its file's bucket."""
    rows = _live_rows(session, "ingestion")
    warnings = []
    if ingestion_class is None and not rows:
        warnings.append(IngestWarning(code="no_live_worker", message="No ingestion worker is live; the request waits in the queue until one is"))
    elif ingestion_class is not None and not any(ingestion_class in row.capabilities for row in rows):
        warnings.append(IngestWarning(code="no_live_worker", ingestion_class=ingestion_class, message=f"No live ingestion worker offers {ingestion_class} (see GET /ingestors)"))
    if rows and bucket and not any(bucket in row.buckets for row in rows):
        warnings.append(IngestWarning(code="store_not_read", bucket=bucket, message=f"No live ingestion worker reads bucket {bucket}"))
    return warnings
