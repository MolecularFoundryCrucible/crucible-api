"""A person's signed-in sessions: one per sign-in through a client (the Hub, the command line on a
laptop or a device), each a token family. Listing and ending them follows OWASP ASVS 5.0 V7.5.2
(view, and end any or all after signing in again) and V7.4.5 (administrators can end one person's
sessions or everyone's). There is no limit on how many a person may have (ASVS V7.1.2)."""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.auth.clients import client_by_id
from app.tables import TokenFamily, User


@dataclass(frozen=True)
class ActiveSession:
    id: str
    client_id: str
    client_name: str
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
    auth_time: datetime | None


def _aware(value: datetime | None) -> datetime | None:
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def _live(family: TokenFamily, now: datetime) -> bool:
    from app.auth.tokens import session_lifetimes

    if family.revoked_at is not None or now >= _aware(family.expires_at) or client_by_id(family.client_id) is None:
        return False
    return now < _aware(family.last_refreshed_at) + session_lifetimes(family.client_id)[1]


def active_sessions(session: Session, user_id: int, *, now: datetime | None = None) -> list[ActiveSession]:
    """This person's sessions that can still be used, newest first."""
    current = _now(now)
    families = session.exec(select(TokenFamily).where(
        TokenFamily.user_id == user_id, TokenFamily.revoked_at == None, TokenFamily.expires_at > current,  # noqa: E711
    ).order_by(TokenFamily.created_at.desc(), TokenFamily.id.desc())).all()
    found = []
    for family in families:
        if not _live(family, current):
            continue
        client = client_by_id(family.client_id)
        found.append(ActiveSession(
            family.id, family.client_id, client.display_name, _aware(family.created_at), _aware(family.last_refreshed_at),
            _aware(family.expires_at), _aware(family.auth_time),
        ))
    return found


def end_session(session: Session, user: User, session_id: str, *, actor: User | None, now: datetime | None = None) -> bool:
    """End one of this person's sessions in the caller's transaction; False if it is not theirs or already over."""
    from app.auth.events import AuthEventType, record_event

    current = _now(now)
    family = session.exec(select(TokenFamily).where(TokenFamily.id == session_id, TokenFamily.user_id == user.id).with_for_update()).one_or_none()
    if family is None or not _live(family, current):
        return False
    family.revoked_at = current
    session.add(family)
    record_event(session, AuthEventType.SESSIONS_ENDED, user=user, actor=actor, client_id=family.client_id)
    return True


def end_sessions(session: Session, user: User, *, actor: User | None, keep: str | None = None, now: datetime | None = None) -> int:
    """End all of this person's sessions but `keep`, in the caller's transaction, and say how many."""
    from app.auth.events import AuthEventType, record_event

    current = _now(now)
    conditions = [TokenFamily.user_id == user.id, TokenFamily.revoked_at == None]  # noqa: E711
    if keep is not None:
        conditions.append(TokenFamily.id != keep)
    ended = session.execute(update(TokenFamily).where(*conditions).values(revoked_at=current)
                            .execution_options(synchronize_session=False).returning(TokenFamily.id)).all()
    if ended:
        record_event(session, AuthEventType.SESSIONS_ENDED, user=user, actor=actor)
    return len(ended)


def end_everyones_sessions(engine, *, commit: bool, now: datetime | None = None) -> int:
    """For an administrator at the server, after a breach: end every session of every person."""
    current = _now(now)
    with Session(engine) as session, session.begin():
        if not commit:
            return len(session.exec(select(TokenFamily.id).where(TokenFamily.revoked_at == None)).all())  # noqa: E711
        return len(session.execute(update(TokenFamily).where(TokenFamily.revoked_at == None).values(revoked_at=current)  # noqa: E711
                                   .execution_options(synchronize_session=False).returning(TokenFamily.id)).all())
