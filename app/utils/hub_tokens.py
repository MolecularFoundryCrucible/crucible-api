import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from app.models.core_models import HubAccessToken, HubRefreshToken, HubTokenFamily, User


ACCESS_LIFETIME = timedelta(minutes=15)
REFRESH_ABSOLUTE_LIFETIME = timedelta(days=30)
REFRESH_IDLE_LIFETIME = timedelta(days=7)
ACCESS_PREFIX = "chba_"
REFRESH_PREFIX = "chbr_"


class InvalidHubGrant(Exception):
    pass


@dataclass(frozen=True)
class HubTokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime
    user_unique_id: str


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token(prefix: str) -> str:
    return prefix + secrets.token_urlsafe(48)


def _issue_access(session: Session, family: HubTokenFamily, now: datetime) -> tuple[str, datetime]:
    token = _token(ACCESS_PREFIX)
    expires_at = min(now + ACCESS_LIFETIME, _aware(family.expires_at))
    session.add(
        HubAccessToken(
            token_hash=_digest(token),
            family_id=family.id,
            issued_at=now,
            expires_at=expires_at,
        )
    )
    return token, expires_at


def issue_hub_tokens(engine, user_unique_id: str, client_id: str, *, now: datetime | None = None, auth_time: datetime | None = None) -> HubTokenPair:
    with Session(engine) as session, session.begin():
        return issue_hub_tokens_in_session(session, user_unique_id, client_id, now=now, auth_time=auth_time)


def issue_hub_tokens_in_session(session: Session, user_unique_id: str, client_id: str, *, now: datetime | None = None, auth_time: datetime | None = None) -> HubTokenPair:
    current = _now(now)
    user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
    if user is None or user.is_service_account:
        raise InvalidHubGrant
    family = HubTokenFamily(
        id=str(uuid.uuid4()),
        user_unique_id=user_unique_id,
        client_id=client_id,
        created_at=current,
        expires_at=current + REFRESH_ABSOLUTE_LIFETIME,
        last_refreshed_at=current,
        auth_time=auth_time,
    )
    session.add(family)
    session.flush()
    refresh_token = _token(REFRESH_PREFIX)
    session.add(
        HubRefreshToken(
            token_hash=_digest(refresh_token),
            family_id=family.id,
            generation=0,
            issued_at=current,
        )
    )
    access_token, access_expires_at = _issue_access(session, family, current)
    return HubTokenPair(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=access_expires_at,
        refresh_expires_at=family.expires_at,
        user_unique_id=user_unique_id,
    )


def refresh_hub_tokens(engine, refresh_token: str, client_id: str, *, now: datetime | None = None) -> HubTokenPair:
    if not refresh_token.startswith(REFRESH_PREFIX):
        raise InvalidHubGrant
    current = _now(now)
    token_hash = _digest(refresh_token)
    replayed = False
    result = None
    with Session(engine) as session, session.begin():
        initial_token = session.get(HubRefreshToken, token_hash)
        if initial_token is None:
            raise InvalidHubGrant
        family = session.exec(
            select(HubTokenFamily)
            .where(HubTokenFamily.id == initial_token.family_id)
            .with_for_update()
        ).one_or_none()
        if family is None or family.client_id != client_id or family.revoked_at is not None:
            raise InvalidHubGrant
        token = session.exec(
            select(HubRefreshToken)
            .where(HubRefreshToken.token_hash == token_hash)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        if token.spent_at is not None:
            family.revoked_at = current
            replayed = True
        elif current >= _aware(family.expires_at) or current >= _aware(family.last_refreshed_at) + REFRESH_IDLE_LIFETIME:
            family.revoked_at = current
        else:
            user = session.exec(select(User).where(User.unique_id == family.user_unique_id)).one_or_none()
            if user is None or user.is_service_account:
                family.revoked_at = current
            else:
                token.spent_at = current
                family.last_refreshed_at = current
                next_refresh = _token(REFRESH_PREFIX)
                session.add(
                    HubRefreshToken(
                        token_hash=_digest(next_refresh),
                        family_id=family.id,
                        generation=token.generation + 1,
                        issued_at=current,
                    )
                )
                access_token, access_expires_at = _issue_access(session, family, current)
                result = HubTokenPair(
                    access_token=access_token,
                    refresh_token=next_refresh,
                    access_expires_at=access_expires_at,
                    refresh_expires_at=_aware(family.expires_at),
                    user_unique_id=family.user_unique_id,
                )
    if replayed or result is None:
        raise InvalidHubGrant
    return result


def hub_access_user(session: Session, access_token: str, *, now: datetime | None = None) -> str:
    return hub_access_family(session, access_token, now=now).user_unique_id


def hub_access_family(session: Session, access_token: str, *, now: datetime | None = None) -> HubTokenFamily:
    if not access_token.startswith(ACCESS_PREFIX):
        raise InvalidHubGrant
    current = _now(now)
    token = session.get(HubAccessToken, _digest(access_token))
    if token is None or current >= _aware(token.expires_at):
        raise InvalidHubGrant
    family = session.get(HubTokenFamily, token.family_id)
    if family is None or family.revoked_at is not None or current >= _aware(family.expires_at):
        raise InvalidHubGrant
    user = session.exec(select(User).where(User.unique_id == family.user_unique_id)).one_or_none()
    if user is None or user.is_service_account:
        raise InvalidHubGrant
    return family


def revoke_hub_tokens(engine, credential: str, client_id: str, *, now: datetime | None = None) -> None:
    if credential.startswith(ACCESS_PREFIX):
        model = HubAccessToken
    elif credential.startswith(REFRESH_PREFIX):
        model = HubRefreshToken
    else:
        return
    with Session(engine) as session, session.begin():
        token = session.get(model, _digest(credential))
        if token is None:
            return
        family = session.exec(
            select(HubTokenFamily)
            .where(HubTokenFamily.id == token.family_id)
            .with_for_update()
        ).one_or_none()
        if family is not None and family.client_id == client_id and family.revoked_at is None:
            family.revoked_at = _now(now)
