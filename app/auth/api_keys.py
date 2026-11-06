"""API keys stored as hashes. While the plain `apitoken` table still exists, every key written there
is written here too, and every revocation reaches both."""
import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import Session

from app.models.core_models import ApiKey

KEY_LIFETIME = timedelta(days=365)


def key_hash(presented: str) -> str:
    return hashlib.sha256(presented.encode("utf-8")).hexdigest()


def store_key(session: Session, user_id: int, key: str, *, created_at: datetime | None = None, name: str | None = None) -> ApiKey:
    """Record a key in the caller's transaction, as its hash and last four characters."""
    created = created_at or datetime.now(timezone.utc)
    row = ApiKey(user_id=user_id, key_hash=key_hash(key), key_last4=key[-4:], name=name, created_at=created, expires_at=created + KEY_LIFETIME)
    session.add(row)
    return row


def revoke_user_keys(session: Session, user_id: int, *, now: datetime | None = None) -> None:
    """Mark every active key of this person revoked, in the caller's transaction. The rows stay."""
    session.execute(update(ApiKey).where(ApiKey.user_id == user_id, ApiKey.revoked_at == None).values(revoked_at=now or datetime.now(timezone.utc)))  # noqa: E711
