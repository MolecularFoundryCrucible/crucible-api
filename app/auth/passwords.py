"""Passwords: Argon2id through pwdlib, with a pepper kept outside the database, and the rules for
choosing one (NIST SP 800-63B-4 3.1.1.2). The order of normalizing and peppering is fixed:
NFC-normalize, then HMAC-SHA256 with the pepper and base64-encode, then Argon2id."""
import base64
import hashlib
import hmac
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from pwdlib import PasswordHash
from pwdlib.exceptions import PwdlibError
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlmodel import Session, select

from app.config import get_secret, get_settings
from app.models.core_models import LocalCredential, User

MAX_LENGTH = 256
SERVICE_NAME = "crucible"
_BLOCKLIST = Path(__file__).parent / "data" / "blocked_passwords.txt"


@dataclass(frozen=True)
class PasswordProblem:
    code: str
    message: str


class ServiceAccountPassword(ValueError):
    """Service accounts never have a password."""


class _UnknownPepper(Exception):
    pass


@lru_cache(maxsize=4)
def _hasher_for(memory_kib: int, time_cost: int, parallelism: int) -> PasswordHash:
    return PasswordHash((Argon2Hasher(memory_cost=memory_kib, time_cost=time_cost, parallelism=parallelism),))


def _hasher() -> PasswordHash:
    auth = get_settings().auth
    return _hasher_for(auth.argon2_memory_kib, auth.argon2_time_cost, auth.argon2_parallelism)


def _current_pepper() -> tuple[str | None, bytes | None]:
    auth = get_settings().auth
    if auth.pepper_id is None or auth.pepper is None or not auth.pepper.get_secret_value():
        return None, None
    return auth.pepper_id, get_secret("CRUCIBLE_AUTH_PEPPER").encode("utf-8")


def _pepper(pepper_id: str | None) -> bytes | None:
    if pepper_id is None:
        return None
    current_id, current = _current_pepper()
    if pepper_id == current_id:
        return current
    if pepper_id in get_settings().auth.previous_pepper_ids:
        return get_secret(f"CRUCIBLE_AUTH_PREVIOUS_PEPPER_{pepper_id.upper()}").encode("utf-8")
    raise _UnknownPepper


def _prepared(password: str, pepper: bytes | None) -> str:
    normalized = unicodedata.normalize("NFC", password)
    if pepper is None:
        return normalized
    return base64.b64encode(hmac.new(pepper, normalized.encode("utf-8"), hashlib.sha256).digest()).decode("ascii")


@lru_cache(maxsize=4)
def _dummy_hash(memory_kib: int, time_cost: int, parallelism: int) -> str:
    return _hasher_for(memory_kib, time_cost, parallelism).hash("not a password anyone has")


def hash_password(password: str) -> tuple[str, str | None]:
    """The PHC-format Argon2id hash of a password under the current pepper, and that pepper's ID."""
    pepper_id, pepper = _current_pepper()
    return _hasher().hash(_prepared(password, pepper)), pepper_id


def verify_dummy(password: str) -> None:
    """The same work as checking a real password, for accounts that have none, so the time taken
    does not tell whether one exists."""
    auth = get_settings().auth
    _hasher().verify(_prepared(password or " ", _current_pepper()[1]), _dummy_hash(auth.argon2_memory_kib, auth.argon2_time_cost, auth.argon2_parallelism))


def verify_password(password: str, stored_hash: str | None, pepper_id: str | None) -> tuple[bool, tuple[str, str | None] | None]:
    """Whether the password matches, and a new hash and pepper ID to store when the hash settings
    or the pepper have changed since it was made. An empty password or hash never matches."""
    if not password or not stored_hash:
        verify_dummy(password)
        return False, None
    try:
        pepper = _pepper(pepper_id)
    except _UnknownPepper:
        verify_dummy(password)
        return False, None
    try:
        valid, updated = _hasher().verify_and_update(_prepared(password, pepper), stored_hash)
    except (PwdlibError, ValueError):
        return False, None
    if not valid:
        return False, None
    if updated is not None or pepper_id != _current_pepper()[0]:
        return True, hash_password(password)
    return True, None


@lru_cache(maxsize=1)
def blocked_passwords() -> frozenset[str]:
    lines = _BLOCKLIST.read_text(encoding="utf-8").splitlines()
    return frozenset(line for line in lines if line and not line.startswith("#"))


def _folded(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def check_new_password(password: str, *, context: Iterable[str | None]) -> PasswordProblem | None:
    """Why a new password is refused, with short advice, or None. Length counts Unicode code points
    after NFC; nothing is trimmed, and there are no rules about character kinds."""
    normalized = unicodedata.normalize("NFC", password)
    shortest = get_settings().auth.password_min_length
    if len(normalized) < shortest:
        return PasswordProblem("too_short", f"Use at least {shortest} characters. A few unrelated words make a strong password that is easy to remember.")
    if len(normalized) > MAX_LENGTH:
        return PasswordProblem("too_long", f"Use at most {MAX_LENGTH} characters.")
    folded = normalized.casefold()
    if folded in blocked_passwords():
        return PasswordProblem("common", "This password is on a list of commonly used passwords, so it is easy to guess. Choose another one.")
    personal = {_folded(value) for value in context if value} | {SERVICE_NAME}
    if folded in personal:
        return PasswordProblem("personal", "Don't use your username, email address, name or the name of this service as your password.")
    return None


def store_password(session: Session, user: User, password: str, *, now: datetime | None = None) -> LocalCredential:
    """Set a person's password in the caller's transaction, cancelling any live password link. The
    only code that writes local_credential, and it refuses service accounts."""
    from app.auth.password_links import void_links

    if user.is_service_account:
        raise ServiceAccountPassword
    void_links(session, user, now=now)
    password_hash, pepper_id = hash_password(password)
    credential = session.get(LocalCredential, user.id) or LocalCredential(user_id=user.id, password_hash=password_hash, password_changed_at=now or datetime.now(timezone.utc))
    credential.password_hash = password_hash
    credential.pepper_id = pepper_id
    credential.password_changed_at = now or datetime.now(timezone.utc)
    session.add(credential)
    return credential


def revoke_all_passwords(engine, *, commit: bool, now: datetime | None = None) -> dict[int, int]:
    """After a breach: remove every password, cancel password links and end the sessions of everyone
    who had one. Without `commit`, only counts. Returns how many passwords there were and how many of
    those people have no other way to sign in, who then need a new link."""
    from sqlalchemy import delete, update

    from app.auth.events import AuthEventType, record_event
    from app.models.core_models import HubTokenFamily, PasswordToken, UserIdentity

    current = now or datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        query = select(LocalCredential.user_id)
        user_ids = session.exec(query.with_for_update() if commit else query).all()
        with_login = set(session.exec(select(UserIdentity.user_id).where(UserIdentity.user_id.in_(user_ids))).all()) if user_ids else set()
        counts = {"passwords": len(user_ids), "without_another_login": len([user_id for user_id in user_ids if user_id not in with_login])}
        if commit and user_ids:
            session.execute(delete(LocalCredential).where(LocalCredential.user_id.in_(user_ids)))
            session.execute(update(PasswordToken).where(PasswordToken.user_id.in_(user_ids), PasswordToken.consumed_at == None, PasswordToken.voided_at == None)  # noqa: E711
                            .values(voided_at=current).execution_options(synchronize_session=False))
            session.execute(update(HubTokenFamily).where(HubTokenFamily.user_id.in_(user_ids), HubTokenFamily.revoked_at == None)  # noqa: E711
                            .values(revoked_at=current).execution_options(synchronize_session=False))
            for user in session.exec(select(User).where(User.id.in_(user_ids))).all():
                record_event(session, AuthEventType.PASSWORD_REMOVED, user=user)
        return counts
