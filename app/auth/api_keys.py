"""API keys stored as hashes. Several per person, each shown once. While the plain `apitoken` table
still exists, keys written there are written here too, every revocation reaches both, and a key
found only there is copied here on first use, so the order of deploy and backfill does not matter."""
import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.core_models import ApiKey, ApiToken, User

KEY_LIFETIME = timedelta(days=365)
KEY_PREFIX = "crucible_"
MAX_ACTIVE_KEYS = 10


class TooManyKeys(Exception):
    pass


def key_hash(presented: str) -> str:
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def created_at_of(token_created: str | None) -> datetime | None:
    """`apitoken.token_created` holds either a date (`YYYY-MM-DD`) or an ISO timestamp."""
    if not token_created:
        return None
    try:
        if len(token_created) == 10:
            return datetime.combine(date.fromisoformat(token_created), datetime.min.time(), tzinfo=timezone.utc)
        moment = datetime.fromisoformat(token_created.replace("Z", "+00:00"))
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def store_key(session: Session, user_id: int, key: str, *, created_at: datetime | None = None, name: str | None = None, expires_at: datetime | None = None) -> ApiKey:
    """Record a key in the caller's transaction, as its hash and last four characters."""
    created = _now(created_at)
    row = ApiKey(user_id=user_id, key_hash=key_hash(key), key_last4=key[-4:], name=name, created_at=created, expires_at=expires_at or created + KEY_LIFETIME)
    session.add(row)
    return row


def status_of(row: ApiKey, now: datetime | None = None) -> str:
    if row.revoked_at is not None:
        return "revoked"
    expires = _aware(row.expires_at)
    return "expired" if expires is not None and expires <= _now(now) else "active"


def active(now: datetime | None = None):
    """SQL test for keys that are neither revoked nor expired."""
    return (ApiKey.revoked_at == None) & or_(ApiKey.expires_at == None, ApiKey.expires_at > _now(now))  # noqa: E711


def _adopt_plain_key(session: Session, presented: str) -> ApiKey | None:
    token = session.exec(select(ApiToken).where(ApiToken.token == presented)).first()
    created = created_at_of(token.token_created) if token is not None else None
    if token is None or token.user_id is None or created is None:
        return None
    try:
        with session.begin_nested():
            row = store_key(session, token.user_id, presented, created_at=created)
            row.last_used_at = token.last_used_at
    except IntegrityError:
        return session.exec(select(ApiKey).where(ApiKey.key_hash == key_hash(presented))).one_or_none()
    return row


def find_api_key(session: Session, presented: str) -> ApiKey | None:
    """The stored key matching what was presented, in any state, by its hash."""
    if not presented or len(presented) > 256:
        return None
    row = session.exec(select(ApiKey).where(ApiKey.key_hash == key_hash(presented))).one_or_none()
    return row if row is not None else _adopt_plain_key(session, presented)


def newest_key(session: Session, user_id: int) -> ApiKey | None:
    """The newest key that is not revoked, expired or not, for status displays."""
    return session.exec(select(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at == None).order_by(ApiKey.created_at.desc(), ApiKey.id.desc())).first()  # noqa: E711


def create_key(session: Session, user: User, *, name: str | None = None, expires_at: datetime | None = None, now: datetime | None = None) -> tuple[ApiKey, str]:
    """A new key, returned once, in the caller's transaction. At most MAX_ACTIVE_KEYS active keys."""
    from app.auth.events import AuthEventType, record_event

    current = _now(now)
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
        row.revoked_at = _now(now)
        session.add(row)
    for token in session.exec(select(ApiToken).where(ApiToken.user_id == row.user_id)).all():
        if key_hash(token.token) == row.key_hash:
            session.delete(token)
    record_event(session, AuthEventType.API_KEY_REVOKED, user=session.get(User, row.user_id), actor=actor)


def revoke_user_keys(session: Session, user_id: int, *, now: datetime | None = None) -> None:
    """Mark every active key of this person revoked, in the caller's transaction. The rows stay."""
    session.execute(update(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at == None).values(revoked_at=_now(now)))  # noqa: E711


def key_status(key: ApiKey | None):
    """The `api_key_status` shape for a key: valid, creation and expiry dates, and last use."""
    from app.models.users import ServiceAccountKeyStatus

    if key is None:
        return None
    created, expires = _aware(key.created_at), _aware(key.expires_at)
    return ServiceAccountKeyStatus(
        valid=status_of(key) == "active",
        created_at=created.date().isoformat(),
        expires_at=(expires or created + KEY_LIFETIME).date().isoformat(),
        last_used_at=key.last_used_at,
    )

