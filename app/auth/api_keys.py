"""API keys stored as hashes. Several per person, each shown once."""
import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlmodel import Session, select

from app.tables import ApiKey, User
from app.auth.common import as_utc, now_or

KEY_LIFETIME = timedelta(days=365)
KEY_PREFIX = "crucible_"
DEFAULT_KEY_NAME = "Default key"
MAX_ACTIVE_KEYS = 10


class TooManyKeys(Exception):
    pass


def key_hash(presented: str) -> str:
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


def store_key(session: Session, user_id: int, key: str, *, created_at: datetime | None = None, name: str | None = None, expires_at: datetime | None = None) -> ApiKey:
    """Record a key in the caller's transaction, as its hash and last four characters."""
    created = now_or(created_at)
    row = ApiKey(user_id=user_id, key_hash=key_hash(key), key_last4=key[-4:], name=name, created_at=created, expires_at=expires_at or created + KEY_LIFETIME)
    session.add(row)
    return row


def status_of(row: ApiKey, now: datetime | None = None) -> str:
    if row.revoked_at is not None:
        return "revoked"
    expires = as_utc(row.expires_at)
    return "expired" if expires is not None and expires <= now_or(now) else "active"


def active(now: datetime | None = None):
    """SQL test for keys that are neither revoked nor expired."""
    return (ApiKey.revoked_at == None) & or_(ApiKey.expires_at == None, ApiKey.expires_at > now_or(now))  # noqa: E711


def find_api_key(session: Session, presented: str) -> ApiKey | None:
    """The stored key matching what was presented, in any state, by its hash."""
    if not presented or len(presented) > 256:
        return None
    return session.exec(select(ApiKey).where(ApiKey.key_hash == key_hash(presented))).one_or_none()


def newest_keys(session: Session, user_ids, now: datetime | None = None) -> dict[int, ApiKey]:
    """For status displays: each person's newest active key, else their newest expired one."""
    current = now_or(now)
    ids = list(user_ids)
    if not ids:
        return {}
    candidates = session.exec(select(ApiKey).where(ApiKey.user_id.in_(ids), ApiKey.revoked_at == None)).all()  # noqa: E711
    best: dict[int, ApiKey] = {}
    for row in candidates:
        rank = (status_of(row, current) == "active", as_utc(row.created_at), row.id or 0)
        held = best.get(row.user_id)
        if held is None or rank > (status_of(held, current) == "active", as_utc(held.created_at), held.id or 0):
            best[row.user_id] = row
    return best


def newest_key(session: Session, user_id: int, now: datetime | None = None) -> ApiKey | None:
    return newest_keys(session, [user_id], now).get(user_id)


def create_key(session: Session, user: User, *, name: str | None = None, expires_at: datetime | None = None, now: datetime | None = None) -> tuple[ApiKey, str]:
    """A new key, returned once, in the caller's transaction. At most MAX_ACTIVE_KEYS active keys."""
    from app.auth.events import AuthEventType, record_event

    current = now_or(now)
    locked = session.exec(select(User).where(User.id == user.id).with_for_update()).one()
    count = len(session.exec(select(ApiKey.id).where(ApiKey.user_id == locked.id, active(current))).all())
    if count >= MAX_ACTIVE_KEYS:
        raise TooManyKeys
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    row = store_key(session, locked.id, key, created_at=current, name=name, expires_at=expires_at)
    record_event(session, AuthEventType.API_KEY_CREATED, user=locked)
    return row, key


def revoke_key(session: Session, row: ApiKey, *, actor: User | None, now: datetime | None = None) -> None:
    """Revoke one key in the caller's transaction, here and in the plain table. The row stays."""
    from app.auth.events import AuthEventType, record_event

    if row.revoked_at is None:
        row.revoked_at = now_or(now)
        session.add(row)
    record_event(session, AuthEventType.API_KEY_REVOKED, user=session.get(User, row.user_id), actor=actor)


def revoke_user_keys(session: Session, user_id: int, *, now: datetime | None = None) -> None:
    """Mark every active key of this person revoked, in the caller's transaction. The rows stay."""
    session.execute(update(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at == None).values(revoked_at=now_or(now)))  # noqa: E711


def replace_keys(session: Session, user: User, *, now: datetime | None = None) -> str:
    """Revoke every key of this person and create one new key, returned once, in the caller's
    transaction. Service accounts hold a single key this way."""
    session.exec(select(User.id).where(User.id == user.id).with_for_update()).one()
    revoke_user_keys(session, user.id, now=now)
    return create_key(session, user, name=DEFAULT_KEY_NAME, now=now)[1]


def key_status(key: ApiKey | None):
    """The `api_key_status` shape for a key: valid, creation and expiry dates, and last use."""
    from app.models.users import ServiceAccountKeyStatus

    if key is None:
        return None
    created, expires = as_utc(key.created_at), as_utc(key.expires_at)
    return ServiceAccountKeyStatus(
        valid=status_of(key) == "active",
        created_at=created.date().isoformat(),
        expires_at=(expires or created + KEY_LIFETIME).date().isoformat(),
        last_used_at=key.last_used_at,
    )

