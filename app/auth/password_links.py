"""One-time links to set or reset a password. An administrator issues one and passes it on, so no
administrator ever knows a password. Only the token's hash is stored, opening a link does not use it
up, and using one signs nobody in."""
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import delete, update
from sqlmodel import Session, select

from app.config import get_settings
from app.tables import DeviceAuthorization, HubLoginCode, LocalCredential, PasswordToken, User

Purpose = Literal["setup", "reset"]


class LinkRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class LiveLink:
    token_hash: str
    user_id: int
    purpose: str
    expires_at: datetime
    username: str | None
    person: str
    context: tuple[str | None, ...]


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def link_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def link_purpose(session: Session, user: User) -> Purpose:
    return "reset" if session.get(LocalCredential, user.id) is not None else "setup"


def check_link_target(user: User) -> None:
    if user.is_service_account:
        raise LinkRefused("service_account", "Service accounts sign in with API keys, not passwords")
    if user.disabled_at is not None:
        raise LinkRefused("account_disabled", "This account is switched off; enable it first")
    if user.pending_at is not None:
        raise LinkRefused("account_pending", "This account waits for approval; approve it first")


def void_links(session: Session, user: User, *, now: datetime | None = None) -> None:
    """Cancel this person's live link, in the caller's transaction."""
    session.execute(update(PasswordToken).where(
        PasswordToken.user_id == user.id, PasswordToken.consumed_at == None, PasswordToken.voided_at == None,  # noqa: E711
    ).values(voided_at=_now(now)).execution_options(synchronize_session=False))


def issue_link(session: Session, user: User, purpose: Purpose, *, actor: User | None, lifetime: timedelta | None = None, now: datetime | None = None) -> tuple[str, datetime]:
    """A new link for this person, replacing any live one, in the caller's transaction. Returns the
    token, shown once, and when it expires."""
    from app.auth.events import AuthEventType, record_event

    current = _now(now)
    session.exec(select(User.id).where(User.id == user.id).with_for_update()).one()
    check_link_target(user)
    void_links(session, user, now=current)
    token = secrets.token_urlsafe(32)
    expires_at = current + (lifetime or timedelta(hours=get_settings().auth.password_link_hours))
    session.add(PasswordToken(
        token_hash=link_token_hash(token), user_id=user.id, purpose=purpose, created_by_user_id=actor.id if actor is not None else None,
        created_at=current, expires_at=expires_at,
    ))
    record_event(session, AuthEventType.LINK_ISSUED, user=user, actor=actor)
    return token, expires_at


def _usable(row: PasswordToken | None, user: User | None, now: datetime) -> bool:
    if row is None or user is None or row.consumed_at is not None or row.voided_at is not None or now >= _aware(row.expires_at):
        return False
    try:
        check_link_target(user)
    except LinkRefused:
        return False
    return True


def live_link(engine, token: str | None, *, now: datetime | None = None) -> LiveLink | None:
    """The link this token names, if it can still be used. Reading it changes nothing."""
    if not token:
        return None
    with Session(engine) as session:
        row = session.get(PasswordToken, link_token_hash(token))
        user = session.get(User, row.user_id) if row is not None else None
        if not _usable(row, user, _now(now)):
            return None
        person = " ".join(part for part in (user.first_name, user.last_name) if part) or user.username or user.unique_id
        return LiveLink(row.token_hash, user.id, row.purpose, _aware(row.expires_at), user.username, person,
                        (user.username, user.email, user.first_name, user.last_name))


def use_link(session: Session, token: str, password: str, *, now: datetime | None = None) -> User:
    """Set the password and use up the link, in the caller's transaction. Also clears any attempt
    lock, ends every session, and refuses sign-in codes and device approvals given before it."""
    from app.auth.events import AuthEventType, record_event
    from app.auth.passwords import store_password
    from app.utils.hub_tokens import revoke_all_hub_tokens

    current = _now(now)
    row = session.exec(select(PasswordToken).where(PasswordToken.token_hash == link_token_hash(token)).with_for_update()).one_or_none()
    user = session.exec(select(User).where(User.id == row.user_id).with_for_update()).one_or_none() if row is not None else None
    if not _usable(row, user, current):
        raise LinkRefused("link_invalid", "This link is not valid or has expired")
    row.consumed_at = current
    session.add(row)
    session.flush()
    first = session.get(LocalCredential, user.id) is None
    credential = store_password(session, user, password, now=current)
    credential.failed_attempts = 0
    credential.trusted_failed_attempts = 0
    credential.blocked_until = None
    credential.locked_at = None
    revoke_all_hub_tokens(session, user.id, now=current)
    session.execute(update(HubLoginCode).where(HubLoginCode.user_id == user.id, HubLoginCode.consumed_at == None)  # noqa: E711
                    .values(consumed_at=current).execution_options(synchronize_session=False))
    session.execute(update(DeviceAuthorization).where(
        DeviceAuthorization.user_id == user.id, DeviceAuthorization.consumed_at == None, DeviceAuthorization.denied_at == None,  # noqa: E711
    ).values(denied_at=current).execution_options(synchronize_session=False))
    record_event(session, AuthEventType.PASSWORD_SET if first else AuthEventType.PASSWORD_RESET, user=user)
    return user


def remove_expired(session: Session, now: datetime, batch_size: int, *, commit: bool) -> int:
    """Links a day past their expiry, used or not."""
    query = select(PasswordToken.token_hash).where(PasswordToken.expires_at <= now - timedelta(days=1)).order_by(PasswordToken.expires_at).limit(batch_size)
    hashes = session.exec(query.with_for_update(skip_locked=True) if commit else query).all()
    if commit and hashes:
        session.execute(delete(PasswordToken).where(PasswordToken.token_hash.in_(hashes)))
    return len(hashes)
