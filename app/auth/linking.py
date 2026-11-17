"""Linking another sign-in method to an account, and removing one. A link is started by the Hub
with the person's session, finished in their browser at the provider, and written only when the
same person confirms it, so neither side of a forged request can attach a login to an account."""
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, update
from sqlmodel import Session, select

from app.auth.password_links import void_links
from app.auth.events import AuthEventType, record_event
from app.auth.identity import IdentityTaken, ensure_identity, find_identity
from app.config import ORCID_ISSUERS, OidcProvider, get_settings
from app.tables import LoginCode, LoginTransaction, User, UserIdentity
from app.models.identities import IdentityRead
from app.auth.login_transactions import LoginConfig, LoginRefused, InvalidLoginRequest, check_login_request
from app.auth.tokens import InvalidGrant, access_token_family, revoke_all_tokens
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from app.auth.common import as_utc, now_or, sha256_hex

LINK_LIFETIME = timedelta(minutes=10)
START_LIFETIME = timedelta(seconds=120)
LINK_STARTS_ALLOWED = 5
LINK_START_WINDOW = timedelta(minutes=10)
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


class TooManyLinks(Exception):
    """Too many links started recently by one person."""


class LinkSessionStale(Exception):
    """The session confirming a link has no sign-in recent enough."""


@dataclass(frozen=True)
class LinkPreview:
    issuer: str
    subject: str
    label: str | None
    status: str


def require_linking_available() -> None:
    if not get_settings().auth.identity_lookup:
        raise LinkingUnavailable


def begin_link(engine, config: LoginConfig, user_id: int, client_id: str, session_family_id: str, provider: OidcProvider, callback_uri: str, hub_state: str, challenge: str, *, code_verifier: str | None = None, now: datetime | None = None) -> str:
    """Record a link for this person, for the client and the session that started it, and return
    the one-time token of its start address. Raises TooManyLinks after LINK_STARTS_ALLOWED starts
    within LINK_START_WINDOW."""
    require_linking_available()
    check_login_request(config, client_id, callback_uri, hub_state, challenge)
    current = now_or(now)
    start = secrets.token_urlsafe(32)
    with Session(engine) as session, session.begin():
        session.exec(select(User.id).where(User.id == user_id).with_for_update()).one()
        recent = session.exec(select(func.count()).select_from(LoginTransaction).where(
            LoginTransaction.user_id == user_id, LoginTransaction.purpose == "link", LoginTransaction.created_at > current - LINK_START_WINDOW,
        )).one()
        if recent >= LINK_STARTS_ALLOWED:
            raise TooManyLinks
        session.add(LoginTransaction(
            session_family_id=session_family_id,
            state_hash=sha256_hex(start),
            start_hash=sha256_hex(start),
            purpose="link",
            user_id=user_id,
            client_id=client_id,
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


def open_link(engine, start: str, *, now: datetime | None = None) -> tuple[LoginTransaction, str, str]:
    """Use a start address once: give the link a fresh provider state and a browser secret for its
    cookie, returning the transaction, the state and the secret."""
    if not start or len(start) > 128:
        raise InvalidLoginRequest
    current = now_or(now)
    state = secrets.token_urlsafe(32)
    browser_secret = secrets.token_urlsafe(32)
    with Session(engine) as session, session.begin():
        transaction = session.exec(select(LoginTransaction).where(LoginTransaction.start_hash == sha256_hex(start)).with_for_update()).one_or_none()
        if transaction is None or transaction.purpose != "link" or transaction.browser_secret_hash is not None:
            raise InvalidLoginRequest
        if transaction.consumed_at is not None or current >= as_utc(transaction.expires_at) or current >= as_utc(transaction.created_at) + START_LIFETIME:
            raise InvalidLoginRequest
        transaction.state_hash = sha256_hex(state)
        transaction.browser_secret_hash = sha256_hex(browser_secret)
        session.add(transaction)
        session.flush()
        session.expunge(transaction)
    return transaction, state, browser_secret


def _label(provider: OidcProvider, subject: str, claims_label: str | None) -> str | None:
    return subject if provider.is_orcid else claims_label


def create_link_code(engine, transaction: LoginTransaction, provider: OidcProvider, subject: str, auth_time: datetime | None, label: str | None, *, now: datetime | None = None) -> str:
    """After the provider callback: a single-use code naming the login, for the Hub to preview and
    confirm. Refuses a login whose provider did not ask the person to sign in again."""
    current = now_or(now)
    if auth_time is None or auth_time < as_utc(transaction.created_at) - AUTH_TIME_LEEWAY:
        raise LoginRefused("login_required")
    code = secrets.token_urlsafe(48)
    with Session(engine) as session, session.begin():
        user = session.get(User, transaction.user_id) if transaction.user_id is not None else None
        if user is None or user.disabled_at is not None or user.is_service_account:
            raise LoginRefused("access_denied")
        session.add(LoginCode(
            code_hash=sha256_hex(code),
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
            session_family_id=transaction.session_family_id,
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


def _link_grant(session: Session, code: str, verifier: str, client_id: str, callback_uri: str, session_token: str, current: datetime, *, lock: bool) -> tuple[LoginCode, User]:
    if not code or len(code) > 128 or not 43 <= len(verifier) <= 128 or not verifier.isascii():
        raise InvalidGrant
    query = select(LoginCode).where(LoginCode.code_hash == sha256_hex(code))
    grant = session.exec(query.with_for_update() if lock else query).one_or_none()
    if grant is None or grant.purpose != "link" or grant.consumed_at is not None or current >= as_utc(grant.expires_at):
        raise InvalidGrant
    if grant.client_id != client_id or grant.callback_uri != callback_uri or not secrets.compare_digest(grant.code_challenge, create_s256_code_challenge(verifier)):
        raise InvalidGrant
    family = access_token_family(session, session_token, now=current)
    if family.user_id != grant.user_id or family.client_id != client_id or grant.session_family_id is None or family.id != grant.session_family_id:
        raise InvalidGrant
    from app.utils.auth_utils import STEP_UP_MAX_AGE

    if family.auth_time is None or current - as_utc(family.auth_time) > timedelta(seconds=STEP_UP_MAX_AGE):
        raise LinkSessionStale
    return grant, session.get(User, grant.user_id)


def preview_link(engine, code: str, verifier: str, client_id: str, callback_uri: str, session_token: str, *, now: datetime | None = None) -> LinkPreview:
    """What a link code would add, for the Hub's confirmation page. Changes nothing."""
    require_linking_available()
    current = now_or(now)
    with Session(engine) as session:
        grant, user = _link_grant(session, code, verifier, client_id, callback_uri, session_token, current, lock=False)
        return LinkPreview(grant.issuer, grant.subject, grant.label, link_status(session, user, grant.issuer, grant.subject))


def confirm_link(engine, code: str, verifier: str, client_id: str, callback_uri: str, session_token: str, *, now: datetime | None = None) -> UserIdentity:
    """Write the link, only for the person who started it. Raises LinkRefused with the status when
    the login cannot be added."""
    require_linking_available()
    current = now_or(now)
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
                record_event(session, AuthEventType.IDENTITY_LINKED, user=user, actor=user, issuer=grant.issuer, client_id=grant.client_id)
                void_links(session, user, now=current)
                session.flush()
                session.expunge(identity)
                return identity
    raise refusal


def sign_in_labels(session: Session, user_ids: list[int]) -> dict[int, list[str]]:
    """How each person signs in, for administrators: `password` when they have one, then each
    login's provider by display name (its issuer when no listed provider matches)."""
    from app.tables import LocalCredential

    names = {provider.issuer.rstrip("/"): provider.display_name for provider in get_settings().auth.providers}
    labels: dict[int, list[str]] = {user_id: [] for user_id in user_ids}
    if not user_ids:
        return labels
    for user_id in session.exec(select(LocalCredential.user_id).where(LocalCredential.user_id.in_(user_ids))).all():
        labels[user_id].append("password")
    for user_id, issuer in session.exec(select(UserIdentity.user_id, UserIdentity.issuer).where(UserIdentity.user_id.in_(user_ids)).order_by(UserIdentity.id)).all():
        label = names.get(issuer.rstrip("/"), "ORCID" if issuer in ORCID_ISSUERS else issuer)
        if label not in labels[user_id]:
            labels[user_id].append(label)
    return labels


def sign_in_methods(session: Session, user: User) -> list[UserIdentity]:
    return session.exec(select(UserIdentity).where(UserIdentity.user_id == user.id).order_by(UserIdentity.id)).all()


def unlink_identity(session: Session, user: User, identity_id: int, *, actor: User, keep_family_id: str | None = None, now: datetime | None = None) -> UserIdentity:
    """Remove one of the person's logins in the caller's transaction. Refuses their last way to sign
    in, a password included, ends their other Hub sessions and cancels links still in progress."""
    from app.tables import LocalCredential

    current = now_or(now)
    methods = sign_in_methods(session, user)
    identity = next((method for method in methods if method.id == identity_id), None)
    if identity is None:
        raise LookupError(identity_id)
    if len(methods) + (session.get(LocalCredential, user.id) is not None) <= 1:
        raise LastSignInMethod
    session.delete(identity)
    revoke_all_tokens(session, user.id, now=current, except_family_id=keep_family_id)
    session.execute(update(LoginTransaction).where(LoginTransaction.user_id == user.id, LoginTransaction.purpose == "link", LoginTransaction.consumed_at == None).values(consumed_at=current))  # noqa: E711
    session.execute(update(LoginCode).where(LoginCode.user_id == user.id, LoginCode.purpose == "link", LoginCode.consumed_at == None).values(consumed_at=current))  # noqa: E711
    record_event(session, AuthEventType.IDENTITY_UNLINKED, user=user, actor=actor, issuer=identity.issuer)
    void_links(session, user, now=current)
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


class MoveRefused(Exception):
    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


def holdings(session: Session, user: User) -> list[str]:
    """What an account holds besides its own identity group, as reasons it must not be emptied."""

    from app.tables import (
        AccessGroupJoinRequest, ApiKey, Dataset, DeletionRequest, Instrument, PostprocessingRun, Project, ResourceACL, Sample, UploadSession,
        UserAccessGroupLink,
    )
    from app.utils.user_utils import identity_group_of

    own_group = identity_group_of(session, user.id)
    own_group_id = own_group.id if own_group is not None else None
    checks = (
        ("datasets", select(func.count()).select_from(Dataset).where(Dataset.owner_user_id == user.id)),
        ("samples", select(func.count()).select_from(Sample).where(Sample.owner_user_id == user.id)),
        ("instruments", select(func.count()).select_from(Instrument).where(Instrument.owner_user_id == user.id)),
        ("projects led", select(func.count()).select_from(Project).where(Project.lead_user_id == user.id)),
        ("group memberships", select(func.count()).select_from(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id, UserAccessGroupLink.accessgroup_id != own_group_id)),
        ("access grants", select(func.count()).select_from(ResourceACL).where(ResourceACL.accessgroup_id == own_group_id)),
        ("used API keys", select(func.count()).select_from(ApiKey).where(ApiKey.user_id == user.id, ApiKey.last_used_at != None)),  # noqa: E711
        ("join requests", select(func.count()).select_from(AccessGroupJoinRequest).where(AccessGroupJoinRequest.requester_user_id == user.id)),
        ("deletion requests", select(func.count()).select_from(DeletionRequest).where(DeletionRequest.requester_user_id == user.id)),
        ("postprocessing runs", select(func.count()).select_from(PostprocessingRun).where(PostprocessingRun.requested_by_user_id == user.id)),
        ("uploads", select(func.count()).select_from(UploadSession).where(UploadSession.user_id == user.id)),
        ("other logins", select(func.count()).select_from(UserIdentity).where(UserIdentity.user_id == user.id)),
    )
    reasons = []
    for name, query in checks:
        count = session.exec(query).one()
        if name == "other logins":
            count -= 1
        if count:
            reasons.append(f"{count} {name}")
    return reasons


def move_identity(session: Session, identity: UserIdentity, target: User, *, now: datetime | None = None) -> User:
    """Move a login off an account that holds nothing else, onto the right account, and switch the
    emptied account off. This is not a merge: it refuses when the source holds anything."""
    current = now_or(now)
    source = session.get(User, identity.user_id, with_for_update=True)
    if source.id == target.id:
        raise MoveRefused(["the login already belongs to that account"])
    if target.is_service_account or target.disabled_at is not None:
        raise MoveRefused(["the target is a service account or switched off"])
    if identity.issuer in ORCID_ISSUERS and session.exec(select(UserIdentity.id).where(UserIdentity.user_id == target.id, UserIdentity.issuer.in_(ORCID_ISSUERS))).first() is not None:
        raise MoveRefused(["the target already has an ORCID"])
    reasons = holdings(session, source)
    if reasons:
        raise MoveRefused([f"the source account holds {reason}" for reason in reasons])
    identity.user_id = target.id
    session.add(identity)
    from app.auth.api_keys import revoke_user_keys

    revoke_user_keys(session, source.id, now=current)
    source.disabled_at = current
    session.add(source)
    revoke_all_tokens(session, source.id, now=current)
    record_event(session, AuthEventType.IDENTITY_UNLINKED, user=source, issuer=identity.issuer)
    record_event(session, AuthEventType.IDENTITY_LINKED, user=target, issuer=identity.issuer)
    record_event(session, AuthEventType.DISABLED, user=source)
    void_links(session, source, now=current)
    void_links(session, target, now=current)
    return source

