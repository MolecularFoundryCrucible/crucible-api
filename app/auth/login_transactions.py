import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

from sqlmodel import Session, select

from app.auth.identity import retry_after_concurrent_first_write
from app.auth.sign_in import WRONG_USER, AccountPending, SignInRefused, record_refused_sign_in, resolve_sign_in
from app.auth.clients import RegisteredClient, client_by_id, registered_clients, sign_in_enabled
from app.config import ConfigurationError, OidcProvider, get_settings, optional_secret, trusted_redirect_uri
from app.tables import LoginCode, LoginTransaction
from app.auth.tokens import TokenPair, InvalidGrant, issue_tokens_in_session
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from app.auth.common import as_utc, now_or, sha256_hex


LOGIN_LIFETIME = timedelta(minutes=10)
CODE_LIFETIME = timedelta(minutes=1)
PUBLIC_CODE_LIFETIME = timedelta(minutes=5)


def code_lifetime(client_id: str) -> timedelta:
    """A public client's code waits behind a confirmation page, so it gets a few minutes."""
    client = client_by_id(client_id)
    return PUBLIC_CODE_LIFETIME if client is not None and client.is_public else CODE_LIFETIME
CHALLENGE_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class InvalidLoginRequest(Exception):
    pass


class SignInUnavailable(Exception):
    pass


class LoginRefused(Exception):
    """The person proved who they are, but no code may be issued. `error` is the RFC 6749 4.1.2.1
    code the Hub receives."""

    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


@dataclass(frozen=True)
class LoginConfig:
    clients: tuple[RegisteredClient, ...]
    public_api_url: str

    def client(self, client_id: str) -> RegisteredClient | None:
        return next((client for client in self.clients if client.client_id == client_id), None)

    def provider_callback_uri(self, provider: OidcProvider) -> str:
        path = "/auth/hub/callback" if provider.legacy_callback else f"/oauth/callback/{provider.key}"
        return f"{self.public_api_url}{path}"


def public_api_url() -> str:
    """The API's public address, which sign-in addresses and OAuth requests are built on."""
    from app.api.dependencies import get_api_url

    value = get_settings().hub.public_api_url or get_api_url()
    if not trusted_redirect_uri(value):
        raise SignInUnavailable
    return value.rstrip("/")


def load_login_config() -> LoginConfig:
    if not sign_in_enabled():
        raise SignInUnavailable
    if not get_settings().auth.providers and not get_settings().auth.password_enabled:
        raise SignInUnavailable
    base = public_api_url()
    clients = registered_clients()
    try:
        secrets_present = all(optional_secret(client.secret_name) for client in clients if client.secret_name is not None)
    except ConfigurationError:
        raise SignInUnavailable from None
    if not secrets_present:
        raise SignInUnavailable
    if not all(client.client_id and client.redirect_uris and all(map(trusted_redirect_uri, client.redirect_uris)) for client in clients):
        raise SignInUnavailable
    return LoginConfig(clients, base)


def valid_challenge(value: str) -> bool:
    return bool(CHALLENGE_PATTERN.fullmatch(value))


def check_login_request(config: LoginConfig, client_id: str, callback_uri: str, hub_state: str, challenge: str) -> None:
    client = config.client(client_id)
    if client is None or not client.allows_redirect(callback_uri) or not valid_challenge(challenge):
        raise InvalidLoginRequest
    if not 16 <= len(hub_state) <= 256 or not hub_state.isascii():
        raise InvalidLoginRequest


def create_login(engine, config: LoginConfig, client_id: str, callback_uri: str, hub_state: str, challenge: str, *, provider_key: str | None = None, code_verifier: str | None = None, required_user: str | None = None, now: datetime | None = None) -> tuple[str, str, str]:
    check_login_request(config, client_id, callback_uri, hub_state, challenge)
    current = now_or(now)
    state = secrets.token_urlsafe(32)
    browser_secret = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    with Session(engine) as session, session.begin():
        session.add(LoginTransaction(
            state_hash=sha256_hex(state),
            browser_secret_hash=sha256_hex(browser_secret),
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


def consume_login(engine, state: str, browser_secret: str | None, *, now: datetime | None = None) -> LoginTransaction:
    if not browser_secret or not state or len(state) > 128 or len(browser_secret) > 128:
        raise InvalidLoginRequest
    current = now_or(now)
    with Session(engine) as session, session.begin():
        transaction = session.exec(select(LoginTransaction).where(LoginTransaction.state_hash == sha256_hex(state)).with_for_update()).one_or_none()
        if transaction is None or transaction.consumed_at is not None or current >= as_utc(transaction.expires_at):
            raise InvalidLoginRequest
        if transaction.browser_secret_hash is None or not secrets.compare_digest(transaction.browser_secret_hash, sha256_hex(browser_secret)):
            raise InvalidLoginRequest
        transaction.consumed_at = current
        session.add(transaction)
        session.flush()
        session.expunge(transaction)
    return transaction


def create_handoff_code(engine, transaction: LoginTransaction, provider: OidcProvider, subject: str, given_name: str | None, family_name: str | None, *, auth_time: datetime | None = None, now: datetime | None = None) -> tuple[str, str]:
    current = now_or(now)
    code = secrets.token_urlsafe(48)

    pending = []

    def write() -> None:
        pending.clear()
        with Session(engine) as session, session.begin():
            user = resolve_sign_in(session, provider.issuer, subject, given_name, family_name, registration=provider.registration, required_user=transaction.required_user, client_id=transaction.client_id)
            if user.pending_at is not None:
                pending.append(True)
                return
            session.add(LoginCode(
                code_hash=sha256_hex(code),
                user_id=user.id,
                user_unique_id=user.unique_id,
                client_id=transaction.client_id,
                callback_uri=transaction.callback_uri,
                code_challenge=transaction.code_challenge,
                created_at=current,
                expires_at=current + code_lifetime(transaction.client_id),
                auth_time=auth_time,
            ))

    try:
        retry_after_concurrent_first_write(write)
    except SignInRefused as refused:
        record_refused_sign_in(engine, provider.issuer)
        raise LoginRefused("login_required" if refused.reason == WRONG_USER else "access_denied") from None
    if pending:
        raise AccountPending
    return code, client_redirect(transaction.callback_uri, {"code": code, "state": transaction.hub_state})


LOCAL_PROVIDER = "password"


def peek_login(engine, state: str, browser_secret: str | None, *, now: datetime | None = None) -> LoginTransaction:
    """The password sign-in this browser started, without using it up, so a mistyped password keeps
    the sign-in going. Raises InvalidLoginRequest like consume_login."""
    if not browser_secret or not state or len(state) > 128 or len(browser_secret) > 128:
        raise InvalidLoginRequest
    current = now_or(now)
    with Session(engine) as session:
        transaction = session.exec(select(LoginTransaction).where(LoginTransaction.state_hash == sha256_hex(state))).one_or_none()
        if transaction is None or transaction.consumed_at is not None or current >= as_utc(transaction.expires_at) or transaction.provider_key != LOCAL_PROVIDER:
            raise InvalidLoginRequest
        if transaction.browser_secret_hash is None or not secrets.compare_digest(transaction.browser_secret_hash, sha256_hex(browser_secret)):
            raise InvalidLoginRequest
        session.expunge(transaction)
    return transaction


def create_local_code(engine, state: str, browser_secret: str, user_id: int, *, now: datetime | None = None) -> str:
    """After a correct password: use up the sign-in and issue its code in one transaction, and
    return the address to send the browser back to."""
    from app.auth.events import AuthEventType, record_event
    from app.tables import User

    current = now_or(now)
    code = secrets.token_urlsafe(48)
    with Session(engine) as session, session.begin():
        transaction = session.exec(select(LoginTransaction).where(LoginTransaction.state_hash == sha256_hex(state)).with_for_update()).one_or_none()
        if transaction is None or transaction.consumed_at is not None or current >= as_utc(transaction.expires_at) or transaction.provider_key != LOCAL_PROVIDER:
            raise InvalidLoginRequest
        if transaction.browser_secret_hash is None or not secrets.compare_digest(transaction.browser_secret_hash, sha256_hex(browser_secret)):
            raise InvalidLoginRequest
        user = session.get(User, user_id)
        transaction.consumed_at = current
        session.add(transaction)
        session.add(LoginCode(
            code_hash=sha256_hex(code), user_id=user.id, user_unique_id=user.unique_id, client_id=transaction.client_id,
            callback_uri=transaction.callback_uri, code_challenge=transaction.code_challenge,
            created_at=current, expires_at=current + code_lifetime(transaction.client_id), auth_time=current,
        ))
        record_event(session, AuthEventType.SIGN_IN, user=user, issuer=public_api_url(), client_id=transaction.client_id)
        callback, hub_state = transaction.callback_uri, transaction.hub_state
    return client_redirect(callback, {"code": code, "state": hub_state})


def client_redirect(callback_uri: str, params: dict[str, str]) -> str:
    """An answer back to a client, codes and errors alike, naming this API as its issuer (RFC 9207),
    so a client that talks to several servers can tell which one answered."""
    try:
        params = {**params, "iss": public_api_url()}
    except SignInUnavailable:
        pass
    return f"{callback_uri}?{urlencode(params)}"


def error_destination(callback_uri: str, hub_state: str, error: str) -> str:
    return client_redirect(callback_uri, {"error": error, "state": hub_state})


def _use_sign_in_code(session: Session, code: str, verifier: str, client_id: str, callback_uri: str, current: datetime) -> LoginCode:
    """Use up a sign-in code in the caller's transaction, after checking its client, callback and
    PKCE verifier; every failure is the same InvalidGrant."""
    if not code or len(code) > 128 or not 43 <= len(verifier) <= 128 or not verifier.isascii():
        raise InvalidGrant
    grant = session.exec(select(LoginCode).where(LoginCode.code_hash == sha256_hex(code)).with_for_update()).one_or_none()
    if grant is None or grant.purpose != "sign_in" or grant.consumed_at is not None or current >= as_utc(grant.expires_at):
        raise InvalidGrant
    if grant.client_id != client_id or grant.callback_uri != callback_uri or not secrets.compare_digest(grant.code_challenge, create_s256_code_challenge(verifier)):
        raise InvalidGrant
    grant.consumed_at = current
    session.add(grant)
    return grant


def redeem_approval_code(engine, code: str, verifier: str, client_id: str, callback_uri: str, *, now: datetime | None = None) -> tuple[int, datetime | None]:
    """Use up a sign-in code issued to one of the API's own pages, with its PKCE verifier, and
    return who signed in and when, without issuing any session."""
    current = now_or(now)
    with Session(engine) as session, session.begin():
        grant = _use_sign_in_code(session, code, verifier, client_id, callback_uri, current)
        return grant.user_id, as_utc(grant.auth_time)


def exchange_handoff_code(engine, code: str, verifier: str, client_id: str, callback_uri: str, *, now: datetime | None = None) -> TokenPair:
    current = now_or(now)
    with Session(engine) as session, session.begin():
        grant = _use_sign_in_code(session, code, verifier, client_id, callback_uri, current)
        return issue_tokens_in_session(session, grant.user_id, client_id, now=current, auth_time=as_utc(grant.auth_time))
