from datetime import datetime, timezone

from sqlmodel import Session, select

from app.auth.events import AuthEventType, record_event
from app.auth.identity import IdentityTaken, ensure_identity, find_identity
from app.config import ORCID_ISSUERS, get_settings
from app.general_utils import is_orcid
from app.models.core_models import User, get_mfid_value
from app.utils.permissions import AccessGroupType, Role
from app.utils.user_utils import provision_user, sql_add_user_to_access_group

NOT_LINKED = "not_linked"
UNLINKED_ORCID = "unlinked_orcid"
WRONG_USER = "wrong_user"
IDENTITY_TAKEN = "identity_taken"
SERVICE_ACCOUNT = "service_account"
DISABLED = "disabled"


class AccountPending(Exception):
    """The person proved who they are, and their account waits for an administrator's approval.
    Raised after the account is saved, so nothing about it is shown before proof of identity."""


class SignInRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _existing_person(session: Session, issuer: str, subject: str) -> User | None:
    """Who this login belongs to. With the lookup setting off, ORCID sign-in still finds people by
    `unique_id`, for the rollout before the backfill has run."""
    orcid_provider = issuer in ORCID_ISSUERS
    if orcid_provider and not get_settings().auth.identity_lookup:
        return session.exec(select(User).where(User.unique_id == subject)).one_or_none()
    # Read the user row first: a simultaneous first sign-in commits the user and identity together,
    # so once its user is visible, the identity read below sees its identity too.
    unlinked = orcid_provider and session.exec(select(User.id).where(User.unique_id == subject)).first() is not None
    identity = find_identity(session, issuer, subject)
    if identity is not None:
        return session.get(User, identity.user_id)
    if unlinked:
        raise SignInRefused(UNLINKED_ORCID)
    return None


def resolve_sign_in(session: Session, issuer: str, subject: str, given_name: str | None, family_name: str | None, *, registration: str, required_user: str | None = None) -> User:
    """The one answer to "who is this person", for every sign-in path. Matches only the exact issuer
    and subject, never email, username or name. Raises SignInRefused. Runs in the caller's transaction.
    `registration` is the provider's rule for someone new: `closed` refuses them, `approval` creates
    an account that waits, and `open` one that works. A returned user with `pending_at` set is
    waiting: callers commit and issue them nothing."""
    user = _existing_person(session, issuer, subject)
    if user is not None and user.disabled_at is not None:
        raise SignInRefused(DISABLED)
    if required_user is not None and (user is None or user.unique_id != required_user):
        raise SignInRefused(WRONG_USER)
    if user is not None and user.is_service_account:
        raise SignInRefused(SERVICE_ACCOUNT)
    orcid_login = issuer in ORCID_ISSUERS
    if user is None and (registration not in ("approval", "open") or (orcid_login and not is_orcid(subject))):
        raise SignInRefused(NOT_LINKED)
    now = datetime.now(timezone.utc)
    registered = user is None
    try:
        if user is None:
            user = provision_user(session, unique_id=subject if orcid_login else get_mfid_value(), first_name=given_name or "", last_name=family_name or "", email=None, username=None, orcid_issuer=issuer if orcid_login else None)
            if registration == "approval":
                user.pending_at = now
        identity = ensure_identity(session, user, issuer, subject)
    except IdentityTaken:
        raise SignInRefused(IDENTITY_TAKEN) from None
    if not user.first_name and given_name:
        user.first_name = given_name
    if not user.last_name and family_name:
        user.last_name = family_name
    sql_add_user_to_access_group(user.unique_id, user.unique_id, session, role=Role.OWNER, group_type=AccessGroupType.IDENTITY)
    identity.verified_at = identity.verified_at or now
    identity.last_used_at = now
    session.add(identity)
    session.add(user)
    if user.pending_at is not None:
        if registered:
            record_event(session, AuthEventType.REGISTERED, user=user, issuer=issuer)
        return user
    record_event(session, AuthEventType.SIGN_IN, user=user, issuer=issuer)
    return user


def record_refused_sign_in(engine, issuer: str) -> None:
    """Refusals roll back the sign-in's own transaction, so their history gets its own."""
    with Session(engine) as session, session.begin():
        record_event(session, AuthEventType.PROVIDER_REFUSED, issuer=issuer)
