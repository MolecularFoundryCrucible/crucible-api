import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, update
from sqlmodel import Session, select

from app.auth.clients import client_by_id
from app.tables import AccessToken, RefreshToken, TokenFamily, User


ACCESS_LIFETIME = timedelta(minutes=15)
REFRESH_ABSOLUTE_LIFETIME = timedelta(days=30)
REFRESH_IDLE_LIFETIME = timedelta(days=7)
PUBLIC_REFRESH_ABSOLUTE_LIFETIME = timedelta(days=90)
PUBLIC_REFRESH_IDLE_LIFETIME = timedelta(days=14)
REUSE_GRACE = timedelta(seconds=30)
AUTHORIZATION_CODE = "authorization_code"
DEVICE_CODE = "urn:ietf:params:oauth:grant-type:device_code"


def session_lifetimes(client_id: str) -> tuple[timedelta, timedelta]:
    """How long a session lasts in all, and without use: longer for a public client such as a CLI on
    a cluster, which should not have to sign in every week."""
    client = client_by_id(client_id)
    if client is not None and client.is_public:
        return PUBLIC_REFRESH_ABSOLUTE_LIFETIME, PUBLIC_REFRESH_IDLE_LIFETIME
    return REFRESH_ABSOLUTE_LIFETIME, REFRESH_IDLE_LIFETIME
ACCESS_PREFIX = "chba_"
REFRESH_PREFIX = "chbr_"


class InvalidGrant(Exception):
    pass


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    user_unique_id: str
    auth_time: datetime | None = None


def _may_hold_tokens(user: User | None) -> bool:
    return user is not None and not user.is_service_account and user.disabled_at is None and user.pending_at is None


def _holder(session: Session, family: TokenFamily) -> User | None:
    return session.get(User, family.user_id) if family.user_id is not None else None


def revoke_all_tokens(session: Session, user_id: int, *, now: datetime | None = None, except_family_id: str | None = None) -> None:
    """End every Hub session of this person, or every one but `except_family_id`, in the caller's transaction."""
    current = _now(now)
    for family in session.exec(select(TokenFamily).where(TokenFamily.user_id == user_id, TokenFamily.revoked_at == None, TokenFamily.id != except_family_id).with_for_update()).all():  # noqa: E711
        family.revoked_at = current
        session.add(family)


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(48)


def _issue_access(session: Session, family: TokenFamily, now: datetime) -> tuple[str, datetime]:
    token = _token(ACCESS_PREFIX)
    expires_at = min(now + ACCESS_LIFETIME, _aware(family.expires_at))
    session.add(
        AccessToken(
            token_hash=_digest(token),
            family_id=family.id,
            issued_at=now,
            expires_at=expires_at,
        )
    )
    return token, expires_at


def issue_tokens(engine, user_id: int, client_id: str, *, now: datetime | None = None, auth_time: datetime | None = None, grant_type: str = AUTHORIZATION_CODE) -> TokenPair:
    with Session(engine) as session, session.begin():
        return issue_tokens_in_session(session, user_id, client_id, now=now, auth_time=auth_time, grant_type=grant_type)


def issue_tokens_in_session(
    session: Session, user_id: int, client_id: str, *, now: datetime | None = None, auth_time: datetime | None = None, grant_type: str = AUTHORIZATION_CODE,
) -> TokenPair:
    current = _now(now)
    user = session.get(User, user_id)
    if not _may_hold_tokens(user):
        raise InvalidGrant
    family = TokenFamily(
        id=str(uuid.uuid4()),
        user_id=user.id,
        user_unique_id=user.unique_id,
        client_id=client_id,
        created_at=current,
        expires_at=current + session_lifetimes(client_id)[0],
        last_refreshed_at=current,
        auth_time=auth_time,
        grant_type=grant_type,
    )
    session.add(family)
    session.flush()
    refresh_token = _token(REFRESH_PREFIX)
    session.add(
        RefreshToken(
            token_hash=_digest(refresh_token),
            family_id=family.id,
            generation=0,
            issued_at=current,
        )
    )
    access_token, access_expires_at = _issue_access(session, family, current)
    return TokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=access_expires_at,
        refresh_expires_at=family.expires_at,
        user_unique_id=user.unique_id,
        auth_time=auth_time,
    )


def _reusable(session: Session, token: RefreshToken, now: datetime) -> bool:
    """A spent token may be used again for REUSE_GRACE if its holder used it and it is the one just
    before the newest, so two processes refreshing at once, or a retry after a lost response, do not
    end the session. A token retired because a sibling was used gets no grace, so a copied token
    cannot keep renewing itself beside the owner's. Any older token still ends the session."""
    if token.retired_at is not None or now - _aware(token.spent_at) > REUSE_GRACE:
        return False
    newest = session.exec(select(func.max(RefreshToken.generation)).where(RefreshToken.family_id == token.family_id)).one()
    return token.generation == newest - 1


def refresh_tokens(engine, refresh_token: str, client_id: str, *, now: datetime | None = None) -> TokenPair:
    if not refresh_token.startswith(REFRESH_PREFIX):
        raise InvalidGrant
    current = _now(now)
    token_hash = _digest(refresh_token)
    replayed = False
    result = None
    with Session(engine) as session, session.begin():
        initial_token = session.get(RefreshToken, token_hash)
        if initial_token is None:
            raise InvalidGrant
        family = session.exec(
            select(TokenFamily)
            .where(TokenFamily.id == initial_token.family_id)
            .with_for_update()
        ).one_or_none()
        if family is None or family.client_id != client_id or family.revoked_at is not None:
            raise InvalidGrant
        token = session.exec(
            select(RefreshToken)
            .where(RefreshToken.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if token.spent_at is not None and not _reusable(session, token, current):
            family.revoked_at = current
            replayed = True
        elif current >= _aware(family.expires_at) or current >= _aware(family.last_refreshed_at) + session_lifetimes(family.client_id)[1]:
            family.revoked_at = current
        else:
            user = _holder(session, family)
            if not _may_hold_tokens(user):
                family.revoked_at = current
            else:
                if token.spent_at is None:
                    token.spent_at = current
                    # Tokens issued in the grace window share a generation: using one retires the rest.
                    session.execute(update(RefreshToken).where(
                        RefreshToken.family_id == family.id, RefreshToken.generation == token.generation, RefreshToken.spent_at == None,  # noqa: E711
                        RefreshToken.token_hash != token.token_hash,
                    ).values(spent_at=current, retired_at=current).execution_options(synchronize_session=False))
                family.last_refreshed_at = current
                next_refresh = _token(REFRESH_PREFIX)
                session.add(
                    RefreshToken(
                        token_hash=_digest(next_refresh),
                        family_id=family.id,
                        generation=token.generation + 1,
                        issued_at=current,
                    )
                )
                access_token, access_expires_at = _issue_access(session, family, current)
                result = TokenPair(
                    access_token=access_token,
                    refresh_token=next_refresh,
                    access_expires_at=access_expires_at,
                    refresh_expires_at=_aware(family.expires_at),
                    user_unique_id=user.unique_id,
                    auth_time=_aware(family.auth_time) if family.auth_time else None,
                )
    if replayed or result is None:
        raise InvalidGrant
    return result


def access_token_user(session: Session, access_token: str, *, now: datetime | None = None) -> int:
    return access_token_family(session, access_token, now=now).user_id


def access_token_family(session: Session, access_token: str, *, now: datetime | None = None) -> TokenFamily:
    if not access_token.startswith(ACCESS_PREFIX):
        raise InvalidGrant
    current = _now(now)
    token = session.get(AccessToken, _digest(access_token))
    if token is None or current >= _aware(token.expires_at):
        raise InvalidGrant
    family = session.get(TokenFamily, token.family_id)
    if family is None or family.revoked_at is not None or current >= _aware(family.expires_at):
        raise InvalidGrant
    if client_by_id(family.client_id) is None:
        raise InvalidGrant
    user = _holder(session, family)
    if not _may_hold_tokens(user):
        raise InvalidGrant
    return family


def revoke_tokens(engine, credential: str, client_id: str, *, now: datetime | None = None) -> None:
    if credential.startswith(ACCESS_PREFIX):
        model = AccessToken
    elif credential.startswith(REFRESH_PREFIX):
        model = RefreshToken
    else:
        return
    with Session(engine) as session, session.begin():
        token = session.get(model, _digest(credential))
        if token is None:
            return
        family = session.exec(
            select(TokenFamily)
            .where(TokenFamily.id == token.family_id)
            .with_for_update()
        ).one_or_none()
        if family is not None and family.client_id == client_id and family.revoked_at is None:
            family.revoked_at = _now(now)
