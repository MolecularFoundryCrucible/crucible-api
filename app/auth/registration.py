"""Accounts that wait for an administrator: approving them, and removing the ones that are rejected
or never acted on. Only a pending account is ever deleted; an active one is switched off instead,
because records point at it."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlmodel import Session, select

from app.auth.events import AuthEventType, record_event
from app.auth.linking import holdings
from app.models.core_models import ApiKey, ApiToken, User, UserAccessGroupLink, UserIdentity

PENDING_LIFETIME = timedelta(days=30)
logger = logging.getLogger(__name__)


class NotPending(Exception):
    pass


class StillReferenced(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def _locked_pending(session: Session, user: User) -> User:
    locked = session.exec(select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)).one()
    if locked.pending_at is None:
        raise NotPending
    return locked


def approve_pending_user(session: Session, user: User, *, actor: User | None) -> User:
    """Let a waiting account in, in the caller's transaction."""
    user = _locked_pending(session, user)
    user.pending_at = None
    session.add(user)
    record_event(session, AuthEventType.APPROVED, user=user, actor=actor)
    return user


def reject_pending_user(session: Session, user: User, *, actor: User | None) -> None:
    """Delete a waiting account in the caller's transaction, freeing its username and login. Raises
    NotPending for an approved account and StillReferenced when anything besides its own group and
    logins points at it, so it changes nothing in either case."""
    from app.utils.user_utils import identity_group_of

    user = _locked_pending(session, user)
    reasons = [reason for reason in holdings(session, user) if not reason.endswith("other logins")]
    if reasons:
        raise StillReferenced(reasons)
    record_event(session, AuthEventType.REJECTED, user=user, actor=actor)
    group = identity_group_of(session, user.id)
    session.execute(delete(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id))
    session.execute(delete(UserIdentity).where(UserIdentity.user_id == user.id))
    session.execute(delete(ApiToken).where(ApiToken.user_id == user.id))
    session.execute(delete(ApiKey).where(ApiKey.user_id == user.id))
    session.flush()
    if group is not None:
        session.delete(group)
    session.delete(user)


def expire_pending_users(engine, *, commit: bool = False, batch_size: int = 1000, now: datetime | None = None) -> int:
    """Remove accounts left waiting longer than PENDING_LIFETIME, through reject_pending_user.
    Without commit, only counts them."""
    cutoff = (now or datetime.now(timezone.utc)) - PENDING_LIFETIME
    with Session(engine) as session:
        stale = session.exec(select(User).where(User.pending_at != None, User.pending_at <= cutoff).order_by(User.pending_at, User.id).limit(batch_size)).all()  # noqa: E711
        if not commit:
            return len(stale)
        removed = 0
        for user in stale:
            try:
                with session.begin_nested():
                    reject_pending_user(session, user, actor=None)
                removed += 1
            except (NotPending, StillReferenced) as refused:
                logger.warning("Kept a stale pending account: %s", type(refused).__name__)
        session.commit()
        return removed
