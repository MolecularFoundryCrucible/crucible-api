from collections.abc import Callable
from typing import TypeVar

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import get_settings
from app.models.core_models import User, UserIdentity

T = TypeVar("T")


class IdentityTaken(ValueError):
    pass


def retry_after_concurrent_first_write(write: Callable[[], T]) -> T:
    """Run `write`, which opens its own transaction, once more if a unique constraint stops it:
    a simultaneous first sign-in created the rows first, and the second run finds them."""
    try:
        return write()
    except IntegrityError:
        return write()


def default_orcid_issuer() -> str:
    settings = get_settings()
    provider = settings.auth.orcid_provider
    return provider.issuer if settings.hub.enabled and provider is not None else settings.api.orcid_issuer


def find_identity(session: Session, issuer: str, subject: str) -> UserIdentity | None:
    return session.exec(select(UserIdentity).where(UserIdentity.issuer == issuer, UserIdentity.subject == subject)).one_or_none()


def ensure_identity(session: Session, user: User, issuer: str, subject: str, *, label: str | None = None, created_by: User | None = None) -> UserIdentity:
    """Link this provider account to the user in the caller's transaction, or return the existing
    link. Raises IdentityTaken if it belongs to someone else, or if the user already has an ORCID."""
    if user.id is None:
        session.flush()
    existing = find_identity(session, issuer, subject)
    if existing is not None:
        if existing.user_id != user.id:
            raise IdentityTaken
        return existing
    row = UserIdentity(
        user_id=user.id,
        issuer=issuer,
        subject=subject,
        label=label,
        created_by_user_id=created_by.id if created_by is not None else None,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        existing = find_identity(session, issuer, subject)
        if existing is not None and existing.user_id == user.id:
            return existing
        raise IdentityTaken from None
    return row
