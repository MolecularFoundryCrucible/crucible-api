import hashlib
import logging
import threading
from datetime import timedelta

from cachetools import TTLCache
from sqlalchemy import event, or_, select, update
from sqlalchemy.orm import Session as OrmSession, attributes
from sqlmodel import Session

from app.general_utils import get_utc_datetime
from app.models.core_models import ApiKey, ApiToken, Dataset, Instrument, Project, Resource, Sample, User

logger = logging.getLogger(__name__)

USER_ACTIVITY_INTERVAL = timedelta(hours=1)
API_KEY_USE_INTERVAL = timedelta(minutes=10)
RECORD_ACTIVITY_INTERVAL = timedelta(hours=1)

_recently_recorded = TTLCache(maxsize=100_000, ttl=API_KEY_USE_INTERVAL.total_seconds())
_recently_recorded_lock = threading.Lock()


def _first_time(key) -> bool:
    with _recently_recorded_lock:
        if key in _recently_recorded:
            return False
        _recently_recorded[key] = True
        return True


def _stale(column, interval, now):
    return or_(column.is_(None), column < now - interval)


def record_caller_activity(engine, user_id: str, api_key: str | None = None) -> None:
    user_due = _first_time(("user", user_id))
    key_due = api_key is not None and _first_time(("key", hashlib.sha256(api_key.encode()).hexdigest()))
    if not (user_due or key_due):
        return
    now = get_utc_datetime()
    try:
        with Session(engine) as session:
            if user_due:
                session.exec(update(User).where(User.unique_id == user_id, _stale(User.last_active_at, USER_ACTIVITY_INTERVAL, now)).values(last_active_at=now))
            if key_due:
                session.exec(update(ApiKey).where(ApiKey.key_hash == hashlib.sha256(api_key.encode()).hexdigest(), _stale(ApiKey.last_used_at, API_KEY_USE_INTERVAL, now)).values(last_used_at=now))
            session.commit()
    except Exception as error:
        logger.warning("Could not record activity for %s: %s", user_id, type(error).__name__)


def _changed(session, obj) -> bool:
    return obj in session.new or obj in session.deleted or session.is_modified(obj, include_collections=False)


def _touched_records(session):
    projects, instruments, datasets, samples = set(), set(), set(), set()
    for obj in list(session.new) + list(session.dirty) + list(session.deleted):
        if isinstance(obj, (Dataset, Sample)) and _changed(session, obj):
            projects.add(obj.project_mfid)
            if isinstance(obj, Dataset):
                instruments.add(obj.instrument_mfid)
        elif isinstance(obj, Resource) and obj not in session.new and attributes.get_history(obj, "modification_time").has_changes():
            {"project": projects, "dataset": datasets, "sample": samples}.get(obj.resource_type, set()).add(obj.mfid)
    with session.no_autoflush:
        if datasets:
            for project_mfid, instrument_mfid in session.execute(select(Dataset.project_mfid, Dataset.instrument_mfid).where(Dataset.unique_id.in_(datasets))):
                projects.add(project_mfid)
                instruments.add(instrument_mfid)
        if samples:
            projects.update(session.execute(select(Sample.project_mfid).where(Sample.unique_id.in_(samples))).scalars())
    projects.discard(None)
    instruments.discard(None)
    return projects, instruments


@event.listens_for(OrmSession, "before_flush")
def _record_record_activity(session, flush_context, instances):
    projects, instruments = _touched_records(session)
    if not (projects or instruments):
        return
    now = get_utc_datetime()
    connection = session.connection()
    if projects:
        connection.execute(update(Project).where(Project.unique_id.in_(projects), _stale(Project.last_activity_at, RECORD_ACTIVITY_INTERVAL, now)).values(last_activity_at=now))
    if instruments:
        connection.execute(update(Instrument).where(Instrument.unique_id.in_(instruments), _stale(Instrument.last_activity_at, RECORD_ACTIVITY_INTERVAL, now)).values(last_activity_at=now))
