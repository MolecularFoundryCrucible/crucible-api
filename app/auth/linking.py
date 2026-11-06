"""Linking another sign-in method to an account, and removing one. A link is started by the Hub
with the person's session, finished in their browser at the provider, and written only when the
same person confirms it, so neither side of a forged request can attach a login to an account."""
import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.auth.events import AuthEventType, record_event
from app.auth.identity import IdentityTaken, ensure_identity, find_identity
from app.config import ORCID_ISSUERS, OidcProvider, get_settings
from app.models.core_models import HubLoginCode, HubLoginTransaction, User, UserIdentity
from app.models.identities import IdentityRead
from app.utils.hub_login import HubLoginConfig, HubLoginRefused, InvalidHubLogin, _aware, _digest, check_login_request
from app.utils.hub_tokens import InvalidHubGrant, hub_access_family, revoke_all_hub_tokens

LINK_LIFETIME = timedelta(minutes=10)
LINK_CODE_LIFETIME = timedelta(minutes=10)
AUTH_TIME_LEEWAY = timedelta(seconds=60)

AVAILABLE = "available"
ALREADY_YOURS = "already_yours"
LINKED_ELSEWHERE = "linked_elsewhere"
SECOND_ORCID = "second_orcid"
ORCID_OF_ANOTHER = "orcid_of_another"
LINKABLE = (AVAILABLE, ALREADY_YOURS)


class LinkingUnavailable(Exception):
    """Linking is refused while sign-in still finds people by unique_id rather than by their logins."""


class LinkRefused(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class LastSignInMethod(Exception):
    pass


@dataclass(frozen=True)
class LinkPreview:
    issuer: str
    subject: str
    label: str | None
    status: str


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def require_linking_available() -> None:
    if not get_settings().auth.identity_lookup:
        raise LinkingUnavailable


def begin_link(engine, config: HubLoginConfig, user_id: int, provider: OidcProvider, callback_uri: str, hub_state: str, challenge: str, *, code_verifier: str | None = None, now: datetime | None = None) -> str:
    """Record a link for this person and return the one-time token of its start address."""
    require_linking_available()
    check_login_request(config, config.client_id, callback_uri, hub_state, challenge)
    current = _now(now)
    start = secrets.token_urlsafe(32)
    with Session(engine) as session, session.begin():
        session.add(HubLoginTransaction(
            state_hash=_digest(start),
            start_hash=_digest(start),
            purpose="link",
            user_id=user_id,
            client_id=config.client_id,
            callback_uri=callback_uri,
            hub_state=hub_state,
            code_challenge=challenge,
            nonce=secrets.token_urlsafe(32),
            provider_key=provider.key,
            provider_code_verifier=code_verifier,
            created_at=current,
            expires_at=current + LINK_LIFETIME,
        ))
    return start


def open_link(engine, start: str, *, now: datetime | None = None) -> tuple[HubLoginTransaction, str, str]:
    """Use a start address once: give the link a fresh provider state and a browser secret for its
    cookie, returning the transaction, the state and the secret."""
    if not start or len(start) > 128:
        raise InvalidHubLogin
    current = _now(now)
    state = secrets.token_urlsafe(32)
    browser_secret = secrets.token_urlsafe(32)
    with Session(engine) as session, session.begin():
        transaction = session.exec(select(HubLoginTransaction).where(HubLoginTransaction.start_hash == _digest(start)).with_for_update()).one_or_none()
        if transaction is None or transaction.purpose != "link" or transaction.browser_secret_hash is not None:
            raise InvalidHubLogin
        if transaction.consumed_at is not None or current >= _aware(transaction.expires_at):
            raise InvalidHubLogin
        transaction.state_hash = _digest(state)
        transaction.browser_secret_hash = _digest(browser_secret)
        session.add(transaction)
        session.flush()
        session.expunge(transaction)
    return transaction, state, browser_secret


def _label(provider: OidcProvider, subject: str, claims_label: str | None) -> str | None:
    return subject if provider.is_orcid else claims_label


def create_link_code(engine, transaction: HubLoginTransaction, provider: OidcProvider, subject: str, auth_time: datetime | None, label: str | None, *, now: datetime | None = None) -> str:
    """After the provider callback: a single-use code naming the login, for the Hub to preview and
    confirm. Refuses a login whose provider did not ask the person to sign in again."""
    current = _now(now)
    if auth_time is None or auth_time < _aware(transaction.created_at) - AUTH_TIME_LEEWAY:
        raise HubLoginRefused("login_required")
    code = secrets.token_urlsafe(48)
    with Session(engine) as session, session.begin():
        user = session.get(User, transaction.user_id) if transaction.user_id is not None else None
        if user is None or user.disabled_at is not None or user.is_service_account:
            raise HubLoginRefused("access_denied")
        session.add(HubLoginCode(
            code_hash=_digest(code),
            purpose="link",
            user_id=user.id,
            user_unique_id=user.unique_id,
            issuer=provider.issuer,
            subject=subject,
            label=_label(provider, subject, label),
            client_id=transaction.client_id,
            callback_uri=transaction.callback_uri,
            code_challenge=transaction.code_challenge,
            created_at=current,
            expires_at=current + LINK_CODE_LIFETIME,
            auth_time=auth_time,
        ))
    return code


def link_status(session: Session, user: User, issuer: str, subject: str) -> str:
    existing = find_identity(session, issuer, subject)
    if existing is not None:
        return ALREADY_YOURS if existing.user_id == user.id else LINKED_ELSEWHERE
    if issuer in ORCID_ISSUERS:
        if session.exec(select(UserIdentity.id).where(UserIdentity.user_id == user.id, UserIdentity.issuer.in_(ORCID_ISSUERS))).first() is not None:
            return SECOND_ORCID
        holder = session.exec(select(User.id).where(User.unique_id == subject)).first()
        if holder is not None and holder != user.id:
            return ORCID_OF_ANOTHER
    return AVAILABLE


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


def _link_grant(session: Session, code: str, verifier: str, client_id: str, callback_uri: str, session_token: str, current: datetime, *, lock: bool) -> tuple[HubLoginCode, User]:
    if not code or len(code) > 128 or not 43 <= len(verifier) <= 128 or not verifier.isascii():
        raise InvalidHubGrant
    query = select(HubLoginCode).where(HubLoginCode.code_hash == _digest(code))
    grant = session.exec(query.with_for_update() if lock else query).one_or_none()
    if grant is None or grant.purpose != "link" or grant.consumed_at is not None or current >= _aware(grant.expires_at):
        raise InvalidHubGrant
    if grant.client_id != client_id or grant.callback_uri != callback_uri or not secrets.compare_digest(grant.code_challenge, _challenge(verifier)):
        raise InvalidHubGrant
    family = hub_access_family(session, session_token, now=current)
    if family.user_id != grant.user_id:
        raise InvalidHubGrant
    return grant, session.get(User, grant.user_id)


def preview_link(engine, code: str, verifier: str, client_id: str, callback_uri: str, session_token: str, *, now: datetime | None = None) -> LinkPreview:
    """What a link code would add, for the Hub's confirmation page. Changes nothing."""
    require_linking_available()
    current = _now(now)
    with Session(engine) as session:
        grant, user = _link_grant(session, code, verifier, client_id, callback_uri, session_token, current, lock=False)
        return LinkPreview(grant.issuer, grant.subject, grant.label, link_status(session, user, grant.issuer, grant.subject))


def confirm_link(engine, code: str, verifier: str, client_id: str, callback_uri: str, session_token: str, *, now: datetime | None = None) -> UserIdentity:
    """Write the link, only for the person who started it. Raises LinkRefused with the status when
    the login cannot be added."""
    require_linking_available()
    current = _now(now)
    with Session(engine) as session, session.begin():
        grant, user = _link_grant(session, code, verifier, client_id, callback_uri, session_token, current, lock=True)
        grant.consumed_at = current
        session.add(grant)
        status = link_status(session, user, grant.issuer, grant.subject)
        if status not in LINKABLE:
            refusal = LinkRefused(status)
        else:
            try:
                identity = ensure_identity(session, user, grant.issuer, grant.subject, label=grant.label)
            except IdentityTaken:
                refusal = LinkRefused(LINKED_ELSEWHERE)
            else:
                identity.verified_at = identity.verified_at or current
                identity.last_used_at = current
                if grant.label:
                    identity.label = grant.label
                session.add(identity)
                record_event(session, AuthEventType.IDENTITY_LINKED, user=user, actor=user, issuer=grant.issuer)
                session.flush()
                session.expunge(identity)
                return identity
    raise refusal


def sign_in_methods(session: Session, user: User) -> list[UserIdentity]:
    return session.exec(select(UserIdentity).where(UserIdentity.user_id == user.id).order_by(UserIdentity.id)).all()


def unlink_identity(session: Session, user: User, identity_id: int, *, actor: User, keep_family_id: str | None = None, now: datetime | None = None) -> UserIdentity:
    """Remove one of the person's logins in the caller's transaction. Refuses their last way to sign
    in, ends their other Hub sessions and cancels links still in progress."""
    current = _now(now)
    methods = sign_in_methods(session, user)
    identity = next((method for method in methods if method.id == identity_id), None)
    if identity is None:
        raise LookupError(identity_id)
    if len(methods) <= 1:
        raise LastSignInMethod
    session.delete(identity)
    revoke_all_hub_tokens(session, user.id, now=current, except_family_id=keep_family_id)
    session.execute(update(HubLoginTransaction).where(HubLoginTransaction.user_id == user.id, HubLoginTransaction.purpose == "link", HubLoginTransaction.consumed_at == None).values(consumed_at=current))  # noqa: E711
    session.execute(update(HubLoginCode).where(HubLoginCode.user_id == user.id, HubLoginCode.purpose == "link", HubLoginCode.consumed_at == None).values(consumed_at=current))  # noqa: E711
    record_event(session, AuthEventType.IDENTITY_UNLINKED, user=user, actor=actor, issuer=identity.issuer)
    return identity


def provider_fields(issuer: str) -> dict:
    provider = next((listed for listed in get_settings().auth.providers if listed.issuer == issuer), None)
    return {"provider_key": provider.key if provider else None, "provider_name": provider.display_name if provider else issuer}


def identity_read(identity: UserIdentity) -> IdentityRead:
    return IdentityRead(
        id=identity.id,
        **provider_fields(identity.issuer),
        label=identity.label,
        orcid=identity.subject if identity.issuer in ORCID_ISSUERS else None,
        verified_at=identity.verified_at,
        created_at=identity.created_at,
        last_used_at=identity.last_used_at,
    )

