import logging
from enum import Enum

from sqlmodel import Session

from app.models.core_models import AuthEvent, User

logger = logging.getLogger(__name__)


class AuthEventType(str, Enum):
    SIGN_IN = "sign_in"
    SIGN_IN_FAILED = "sign_in_failed"
    PROVIDER_REFUSED = "provider_refused"
    LOCKED = "locked"
    PASSWORD_SET = "password_set"
    PASSWORD_CHANGED = "password_changed"
    PASSWORD_RESET = "password_reset"
    LINK_ISSUED = "link_issued"
    IDENTITY_LINKED = "identity_linked"
    IDENTITY_UNLINKED = "identity_unlinked"
    API_KEY_CREATED = "api_key_created"
    API_KEY_REVOKED = "api_key_revoked"
    DISABLED = "disabled"
    ROLE_CHANGED = "role_changed"
    REGISTERED = "registered"
    APPROVED = "approved"
    REJECTED = "rejected"
    ENABLED = "enabled"


def record_event(session: Session, event: AuthEventType, *, user: User | None = None, actor: User | None = None, issuer: str | None = None, client_address: str | None = None, client_id: str | None = None) -> None:
    """Add to the sign-in history in the caller's transaction. The parameters allow no free text,
    so a password or token cannot reach this table."""
    session.add(AuthEvent(
        event=event.value,
        user_id=user.id if user is not None else None,
        user_unique_id=user.unique_id if user is not None else None,
        actor_user_id=actor.id if actor is not None else None,
        issuer=issuer,
        client_address=client_address,
        client_id=client_id,
    ))
    logger.info("auth event %s user_id=%s actor_user_id=%s", event.value, user.id if user is not None else None, actor.id if actor is not None else None)
