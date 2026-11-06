import base64
import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from sqlmodel import Session, select

from app.auth.identity import retry_after_concurrent_first_write
from app.auth.sign_in import WRONG_USER, AccountPending, SignInRefused, record_refused_sign_in, resolve_sign_in
from app.auth.clients import RegisteredClient, registered_clients, sign_in_enabled
from app.config import ConfigurationError, OidcProvider, get_settings, optional_secret, trusted_redirect_uri
from app.models.core_models import HubLoginCode, HubLoginTransaction
from app.utils.hub_tokens import HubTokenPair, InvalidHubGrant, issue_hub_tokens_in_session


LOGIN_LIFETIME = timedelta(minutes=10)
CODE_LIFETIME = timedelta(minutes=1)
CHALLENGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


def hub_auth_enabled() -> bool:
    return sign_in_enabled()


class InvalidHubLogin(Exception):
    pass


class HubLoginUnavailable(Exception):
    pass


class HubLoginRefused(Exception):
    """The person proved who they are, but no code may be issued. `error` is the RFC 6749 4.1.2.1
    code the Hub receives."""

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


@dataclass(frozen=True)
class HubLoginConfig:
    clients: tuple[RegisteredClient, ...]
    public_api_url: str

    def client(self, client_id: str) -> RegisteredClient | None:
        return next((client for client in self.clients if client.client_id == client_id), None)

    def provider_callback_uri(self, provider: OidcProvider) -> str:
        path = "/auth/hub/callback" if provider.legacy_callback else f"/auth/hub/oidc/{provider.key}/callback"
        return f"{self.public_api_url}{path}"


def public_api_url() -> str:
    """The API's public address, which sign-in addresses and OAuth requests are built on."""
    from app.api.dependencies import get_api_url

    value = get_settings().hub.public_api_url or get_api_url()
    if not trusted_redirect_uri(value):
        raise HubLoginUnavailable
    return value.rstrip("/")


def load_login_config() -> HubLoginConfig:
    if not hub_auth_enabled():
        raise HubLoginUnavailable
    if not get_settings().auth.providers:
        raise HubLoginUnavailable
    base = public_api_url()
    clients = registered_clients()
    try:
        secrets_present = all(optional_secret(client.secret_name) for client in clients)
    except ConfigurationError:
        raise HubLoginUnavailable from None
    if not secrets_present:
        raise HubLoginUnavailable
    if not all(client.client_id and client.redirect_uris and all(map(trusted_redirect_uri, client.redirect_uris)) for client in clients):
        raise HubLoginUnavailable
    return HubLoginConfig(clients, base)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def valid_challenge(value: str) -> bool:
    return bool(CHALLENGE_PATTERN.fullmatch(value))


def check_login_request(config: HubLoginConfig, client_id: str, callback_uri: str, hub_state: str, challenge: str) -> None:
    client = config.client(client_id)
    if client is None or callback_uri not in client.redirect_uris or not valid_challenge(challenge):
        raise InvalidHubLogin
    if not 16 <= len(hub_state) <= 256 or not hub_state.isascii():
        raise InvalidHubLogin


def create_login(engine, config: HubLoginConfig, client_id: str, callback_uri: str, hub_state: str, challenge: str, *, provider_key: str | None = None, code_verifier: str | None = None, required_user: str | None = None, now: datetime | None = None) -> tuple[str, str, str]:
    check_login_request(config, client_id, callback_uri, hub_state, challenge)
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
            provider_key=provider_key,
            provider_code_verifier=code_verifier,
            required_user=required_user,
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
        if transaction.browser_secret_hash is None or not secrets.compare_digest(transaction.browser_secret_hash, _digest(browser_secret)):
            raise InvalidHubLogin
        transaction.consumed_at = current
        session.add(transaction)
        session.flush()
        session.expunge(transaction)
    return transaction


def create_handoff_code(engine, transaction: HubLoginTransaction, provider: OidcProvider, subject: str, given_name: str | None, family_name: str | None, *, auth_time: datetime | None = None, now: datetime | None = None) -> tuple[str, str]:
    current = _now(now)
    code = secrets.token_urlsafe(48)

    pending = []

    def write() -> None:
        pending.clear()
        with Session(engine) as session, session.begin():
            user = resolve_sign_in(session, provider.issuer, subject, given_name, family_name, registration=provider.registration, required_user=transaction.required_user)
            if user.pending_at is not None:
                pending.append(True)
                return
            session.add(HubLoginCode(
                code_hash=_digest(code),
                user_id=user.id,
                user_unique_id=user.unique_id,
                client_id=transaction.client_id,
                callback_uri=transaction.callback_uri,
                code_challenge=transaction.code_challenge,
                created_at=current,
                expires_at=current + CODE_LIFETIME,
                auth_time=auth_time,
            ))

    try:
        retry_after_concurrent_first_write(write)
    except SignInRefused as refused:
        record_refused_sign_in(engine, provider.issuer)
        raise HubLoginRefused("login_required" if refused.reason == WRONG_USER else "access_denied") from None
    if pending:
        raise AccountPending
    return code, f"{transaction.callback_uri}?{urlencode({'code': code, 'state': transaction.hub_state})}"


def hub_error_destination(callback_uri: str, hub_state: str, error: str) -> str:
    return f"{callback_uri}?{urlencode({'error': error, 'state': hub_state})}"


def exchange_handoff_code(engine, code: str, verifier: str, client_id: str, callback_uri: str, *, now: datetime | None = None) -> HubTokenPair:
    if not code or len(code) > 128 or not 43 <= len(verifier) <= 128 or not verifier.isascii():
        raise InvalidHubGrant
    current = _now(now)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    with Session(engine) as session, session.begin():
        grant = session.exec(select(HubLoginCode).where(HubLoginCode.code_hash == _digest(code)).with_for_update()).one_or_none()
        if grant is None or grant.purpose != "sign_in" or grant.consumed_at is not None or current >= _aware(grant.expires_at):
            raise InvalidHubGrant
        if grant.client_id != client_id or grant.callback_uri != callback_uri or not secrets.compare_digest(grant.code_challenge, challenge):
            raise InvalidHubGrant
        grant.consumed_at = current
        return issue_hub_tokens_in_session(session, grant.user_id, client_id, now=current, auth_time=_aware(grant.auth_time) if grant.auth_time else None)
