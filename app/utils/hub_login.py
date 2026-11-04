import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse

from sqlmodel import Session, select

from app.auth.identity import ensure_identity, retry_after_concurrent_first_write
from app.config import ConfigurationError, get_settings, optional_secret
from app.models.core_models import HubLoginCode, HubLoginTransaction, User
from app.utils.hub_tokens import HubTokenPair, InvalidHubGrant, issue_hub_tokens_in_session
from app.utils.permissions import AccessGroupType, Role
from app.utils.user_utils import sql_add_user_to_access_group


LOGIN_LIFETIME = timedelta(minutes=10)
CODE_LIFETIME = timedelta(minutes=1)
CHALLENGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
ORCID_PATTERN = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]\Z")


def hub_auth_enabled() -> bool:
    return get_settings().hub.enabled


class InvalidHubLogin(Exception):
    pass


class HubLoginUnavailable(Exception):
    pass


@dataclass(frozen=True)
class HubLoginConfig:
    client_id: str
    callback_uri: str
    public_api_url: str
    orcid_issuer: str

    @property
    def allowed_callbacks(self) -> tuple[str, ...]:
        return tuple(uri.strip() for uri in self.callback_uri.split(",") if uri.strip())

    @property
    def orcid_callback_uri(self) -> str:
        return f"{self.public_api_url}/auth/hub/callback"


def load_login_config() -> HubLoginConfig:
    from app.api.dependencies import get_api_url

    if not hub_auth_enabled():
        raise HubLoginUnavailable
    hub = get_settings().hub
    client_id = hub.client_id
    callback_uri = hub.callback_uri
    public_api_url = hub.public_api_url or get_api_url()
    issuer = hub.orcid_issuer
    try:
        client_secret = optional_secret("HUB_AUTH_CLIENT_SECRET")
    except ConfigurationError:
        raise HubLoginUnavailable from None
    if not client_id or not client_secret:
        raise HubLoginUnavailable
    if issuer not in {"https://orcid.org", "https://sandbox.orcid.org"}:
        raise HubLoginUnavailable
    config = HubLoginConfig(client_id, callback_uri, public_api_url.rstrip("/"), issuer)
    if not config.allowed_callbacks or not all(map(_trusted_public_url, config.allowed_callbacks)) or not _trusted_public_url(public_api_url):
        raise HubLoginUnavailable
    return config


def _trusted_public_url(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.username or parsed.password or parsed.fragment or parsed.query or not parsed.hostname:
        return False
    return parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"})


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def valid_challenge(value: str) -> bool:
    return bool(CHALLENGE_PATTERN.fullmatch(value))


def create_login(engine, config: HubLoginConfig, client_id: str, callback_uri: str, hub_state: str, challenge: str, *, now: datetime | None = None) -> tuple[str, str, str]:
    if client_id != config.client_id or callback_uri not in config.allowed_callbacks or not valid_challenge(challenge):
        raise InvalidHubLogin
    if not 16 <= len(hub_state) <= 256 or not hub_state.isascii():
        raise InvalidHubLogin
    current = _now(now)
    state = secrets.token_urlsafe(32)
    browser_secret = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    with Session(engine) as session, session.begin():
        session.add(HubLoginTransaction(
            state_hash=_digest(state),
            browser_secret_hash=_digest(browser_secret),
            client_id=client_id,
            callback_uri=callback_uri,
            hub_state=hub_state,
            code_challenge=challenge,
            nonce=nonce,
            created_at=current,
            expires_at=current + LOGIN_LIFETIME,
        ))
    return state, browser_secret, nonce


def consume_login(engine, state: str, browser_secret: str | None, *, now: datetime | None = None) -> HubLoginTransaction:
    if not browser_secret or not state or len(state) > 128 or len(browser_secret) > 128:
        raise InvalidHubLogin
    current = _now(now)
    with Session(engine) as session, session.begin():
        transaction = session.exec(select(HubLoginTransaction).where(HubLoginTransaction.state_hash == _digest(state)).with_for_update()).one_or_none()
        if transaction is None or transaction.consumed_at is not None or current >= _aware(transaction.expires_at):
            raise InvalidHubLogin
        if not secrets.compare_digest(transaction.browser_secret_hash, _digest(browser_secret)):
            raise InvalidHubLogin
        transaction.consumed_at = current
        session.add(transaction)
        session.flush()
        session.expunge(transaction)
    return transaction


def create_handoff_code(engine, transaction: HubLoginTransaction, orcid: str, given_name: str | None, family_name: str | None, *, auth_time: datetime | None = None, now: datetime | None = None) -> tuple[str, str]:
    if not ORCID_PATTERN.fullmatch(orcid):
        raise InvalidHubLogin
    current = _now(now)
    code = secrets.token_urlsafe(48)

    def write() -> None:
        with Session(engine) as session, session.begin():
            user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
            if user is None:
                user = User(unique_id=orcid, first_name=given_name or "", last_name=family_name or "")
                session.add(user)
                session.flush()
            if user.is_service_account:
                raise InvalidHubLogin
            if not user.first_name and given_name:
                user.first_name = given_name
            if not user.last_name and family_name:
                user.last_name = family_name
            sql_add_user_to_access_group(orcid, orcid, session, role=Role.OWNER, group_type=AccessGroupType.IDENTITY)
            ensure_identity(session, user, get_settings().hub.orcid_issuer, orcid)
            session.add(HubLoginCode(
                code_hash=_digest(code),
                user_unique_id=orcid,
                client_id=transaction.client_id,
                callback_uri=transaction.callback_uri,
                code_challenge=transaction.code_challenge,
                created_at=current,
                expires_at=current + CODE_LIFETIME,
                auth_time=auth_time,
            ))

    retry_after_concurrent_first_write(write)
    return code, f"{transaction.callback_uri}?{urlencode({'code': code, 'state': transaction.hub_state})}"


def exchange_handoff_code(engine, code: str, verifier: str, client_id: str, callback_uri: str, *, now: datetime | None = None) -> HubTokenPair:
    if not code or len(code) > 128 or not 43 <= len(verifier) <= 128 or not verifier.isascii():
        raise InvalidHubGrant
    current = _now(now)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    with Session(engine) as session, session.begin():
        grant = session.exec(select(HubLoginCode).where(HubLoginCode.code_hash == _digest(code)).with_for_update()).one_or_none()
        if grant is None or grant.consumed_at is not None or current >= _aware(grant.expires_at):
            raise InvalidHubGrant
        if grant.client_id != client_id or grant.callback_uri != callback_uri or not secrets.compare_digest(grant.code_challenge, challenge):
            raise InvalidHubGrant
        grant.consumed_at = current
        return issue_hub_tokens_in_session(session, grant.user_unique_id, client_id, now=current, auth_time=_aware(grant.auth_time) if grant.auth_time else None)
