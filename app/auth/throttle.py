"""Limits on password attempts (NIST SP 800-63B-4 3.2.2; OWASP Authentication Cheat Sheet).

Each attempt first counts itself as a failure and checks the limits in one atomic UPDATE, then the
password is hashed with no lock or connection held, and a success undoes the count. So parallel
guesses cannot slip past a counter, and a crash during hashing counts as a failure.

- Unknown browsers: 4 failures without delay, then 2, 4, 8 seconds and so on up to 15 minutes; at 90
  they are refused. An attempt during a delay is not counted and its password is not checked.
- A known browser (its device cookie) or a signed-in session: no delays, and at 10 the password is
  disabled until an administrator issues a new link. Both counters together never pass 100.
- Per client address, across accounts: 10 failures without delay, then delays of 2, 4, 8 seconds and
  so on up to one minute. It slows guessing without locking out everyone behind a shared address, and
  a known browser skips it. The count restarts after an hour without failures. A success gives back
  only its own count, so an attacker's own account cannot clear earlier failures.
"""
import hashlib
import hmac
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from itsdangerous import BadData, URLSafeTimedSerializer
from sqlalchemy import case, delete, or_, update
from sqlalchemy.dialects import postgresql, sqlite
from sqlmodel import Session, select

from app.config import get_secret, get_settings
from app.models.core_models import LocalCredential, LoginThrottle

UNKNOWN_FREE_FAILURES = 4
UNKNOWN_LIMIT = 90
KNOWN_LIMIT = 10
TOTAL_LIMIT = 100
ADDRESS_FREE_FAILURES = 10
ADDRESS_WINDOW = timedelta(hours=1)
ADDRESS_RETENTION = timedelta(days=30)
FIRST_DELAY = timedelta(seconds=2)
LONGEST_DELAY = timedelta(minutes=15)
LONGEST_ADDRESS_DELAY = timedelta(minutes=1)
DEVICE_COOKIE_SALT = "crucible.device-cookie"


class HashBusy(Exception):
    """Every password-check slot in this worker stayed taken."""


@dataclass(frozen=True)
class DeviceCookie:
    user_id: int
    password_changed_at: datetime


@dataclass(frozen=True)
class AttemptDecision:
    check_password: bool
    user_id: int | None = None
    trusted: bool = False
    address_hash: str | None = None
    password_hash: str | None = None
    pepper_id: str | None = None
    password_changed_at: datetime | None = None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def delay_after(failures: int, free: int, longest: timedelta = LONGEST_DELAY) -> timedelta | None:
    if failures <= free:
        return None
    doublings = failures - free - 1
    return longest if FIRST_DELAY * 2 ** min(doublings, 20) >= longest else FIRST_DELAY * 2 ** doublings


def address_hash(address: str | None) -> str | None:
    """A keyed hash of the client address, so the table holds no addresses."""
    if not address:
        return None
    return hmac.new(get_secret("CRUCIBLE_SECRET_KEY").encode("utf-8"), b"login-throttle\0" + address.encode("utf-8"), hashlib.sha256).hexdigest()


def client_address(request) -> str | None:
    """The client's address as the server sees it. Behind a proxy, gunicorn and uvicorn take it from
    X-Forwarded-For only for the proxies listed in FORWARDED_ALLOW_IPS."""
    return request.client.host if request.client else None


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_secret("CRUCIBLE_SECRET_KEY"), salt=DEVICE_COOKIE_SALT)


def issue_device_cookie(user_id: int, password_changed_at: datetime) -> str:
    """OWASP's device cookie: proof this browser signed in with this password. Issued anew at each
    sign-in; a password change cancels it, since it carries the change time."""
    return _serializer().dumps({"u": user_id, "p": _aware(password_changed_at).isoformat()})


def read_device_cookie(value: str | None, user_id: int) -> DeviceCookie | None:
    if not value or len(value) > 512:
        return None
    try:
        data = _serializer().loads(value, max_age=get_settings().auth.device_cookie_days * 86400)
        if not isinstance(data, dict) or data.get("u") != user_id or not isinstance(data.get("p"), str):
            return None
        return DeviceCookie(user_id, _aware(datetime.fromisoformat(data["p"])))
    except (BadData, ValueError, TypeError):
        return None


def _insert(session: Session):
    return postgresql.insert if session.get_bind().dialect.name == "postgresql" else sqlite.insert


def _count_address(session: Session, digest: str, now: datetime) -> bool:
    """Count a failure for the address unless it is delayed; False when it is."""
    stmt = _insert(session)(LoginThrottle).values(address_hash=digest, consecutive_failures=1, last_failure_at=now)
    stmt = stmt.on_conflict_do_update(
        index_elements=["address_hash"],
        set_={
            "consecutive_failures": case((LoginThrottle.last_failure_at >= now - ADDRESS_WINDOW, LoginThrottle.consecutive_failures + 1), else_=1),
            "last_failure_at": now,
        },
        where=or_(LoginThrottle.blocked_until.is_(None), LoginThrottle.blocked_until <= now),
    ).returning(LoginThrottle.consecutive_failures)
    row = session.execute(stmt).first()
    if row is None:
        return False
    wait = delay_after(row[0], ADDRESS_FREE_FAILURES, LONGEST_ADDRESS_DELAY)
    session.execute(update(LoginThrottle).where(LoginThrottle.address_hash == digest).values(blocked_until=now + wait if wait else None))
    return True


def begin_attempt(engine, credential_user_id: int | None, client_address: str | None, *, device_cookie: DeviceCookie | None = None, session_authenticated: bool = False, now: datetime | None = None) -> AttemptDecision:
    """Count the attempt and decide whether its password may be checked, in one short transaction."""
    current = now or datetime.now(timezone.utc)
    digest = address_hash(client_address)
    with Session(engine) as session, session.begin():
        credential = session.get(LocalCredential, credential_user_id) if credential_user_id is not None else None
        trusted = credential is not None and (session_authenticated or (
            device_cookie is not None and device_cookie.user_id == credential.user_id and device_cookie.password_changed_at == _aware(credential.password_changed_at)))
        if trusted:
            digest = None
        elif digest is not None and not _count_address(session, digest, current):
            return AttemptDecision(False, credential_user_id, address_hash=digest)
        if credential is None or credential.locked_at is not None:
            return AttemptDecision(False, credential_user_id, address_hash=digest)
        user_id, seen = credential.user_id, credential.password_changed_at
        conditions = [
            LocalCredential.user_id == credential.user_id,
            LocalCredential.locked_at.is_(None),
            LocalCredential.password_changed_at == seen,
            LocalCredential.failed_attempts + LocalCredential.trusted_failed_attempts < TOTAL_LIMIT,
        ]
        if trusted:
            values = {"trusted_failed_attempts": LocalCredential.trusted_failed_attempts + 1}
        else:
            conditions += [LocalCredential.failed_attempts < UNKNOWN_LIMIT, or_(LocalCredential.blocked_until.is_(None), LocalCredential.blocked_until <= current)]
            values = {"failed_attempts": LocalCredential.failed_attempts + 1}
        row = session.execute(update(LocalCredential).where(*conditions).values(**values).execution_options(synchronize_session=False).returning(
            LocalCredential.failed_attempts, LocalCredential.trusted_failed_attempts, LocalCredential.password_hash, LocalCredential.pepper_id,
        )).first()
        if row is None:
            return AttemptDecision(False, credential.user_id, trusted, digest)
        failed, trusted_failed, password_hash, pepper_id = row
        after = {}
        wait = None if trusted else delay_after(failed, UNKNOWN_FREE_FAILURES)
        if wait:
            after["blocked_until"] = current + wait
        if trusted_failed >= KNOWN_LIMIT or failed + trusted_failed >= TOTAL_LIMIT:
            after["locked_at"] = current
        if after:
            session.execute(update(LocalCredential).where(LocalCredential.user_id == user_id).values(**after).execution_options(synchronize_session=False))
    return AttemptDecision(True, user_id, trusted, digest, password_hash, pepper_id, seen)


def begin_code_attempt(engine, client_address: str | None, *, now: datetime | None = None) -> tuple[bool, str | None]:
    """Count a code typed at a sign-in page against the address's limit, before checking it.
    Returns whether it may be checked, and the address's key for giving the count back."""
    current = now or datetime.now(timezone.utc)
    digest = address_hash(client_address)
    if digest is None:
        return True, None
    with Session(engine) as session, session.begin():
        return _count_address(session, digest, current), digest


def give_back(engine, digest: str | None) -> None:
    """Undo one counted failure for an address, after a correct code."""
    if digest is None:
        return
    with Session(engine) as session, session.begin():
        session.execute(update(LoginThrottle).where(LoginThrottle.address_hash == digest, LoginThrottle.consecutive_failures > 0)
                        .values(consecutive_failures=LoginThrottle.consecutive_failures - 1).execution_options(synchronize_session=False))


def finish_attempt(engine, decision: AttemptDecision, *, success: bool, new_hash: tuple[str, str | None] | None = None) -> bool:
    """Undo the count of a successful attempt, and store a rehash if one is due. A success stands
    only if the password has not changed since the attempt began."""
    if not (decision.check_password and success):
        return False
    with Session(engine) as session, session.begin():
        values = {"failed_attempts": 0, "trusted_failed_attempts": 0, "blocked_until": None, "locked_at": None}
        if new_hash is not None:
            values.update(password_hash=new_hash[0], pepper_id=new_hash[1])
        row = session.execute(update(LocalCredential).where(
            LocalCredential.user_id == decision.user_id, LocalCredential.password_changed_at == decision.password_changed_at,
        ).values(**values).execution_options(synchronize_session=False).returning(LocalCredential.user_id)).first()
        if decision.address_hash is not None:
            session.execute(update(LoginThrottle).where(LoginThrottle.address_hash == decision.address_hash, LoginThrottle.consecutive_failures > 0)
                            .values(consecutive_failures=LoginThrottle.consecutive_failures - 1))
        return row is not None


_slots: threading.BoundedSemaphore | None = None
_slots_lock = threading.Lock()


def _semaphore() -> threading.BoundedSemaphore:
    global _slots
    with _slots_lock:
        if _slots is None:
            _slots = threading.BoundedSemaphore(get_settings().auth.hash_concurrency)
        return _slots


@contextmanager
def hash_slot(timeout: float = 30):
    """At most CRUCIBLE_AUTH_HASH_CONCURRENCY password checks at once in this worker, so a burst of
    attempts cannot exhaust memory."""
    slots = _semaphore()
    if not slots.acquire(timeout=timeout):
        raise HashBusy
    try:
        yield
    finally:
        slots.release()


def attempt_password(engine, user_id: int | None, password: str, client_address: str | None, *, device_cookie: DeviceCookie | None = None, session_authenticated: bool = False, now: datetime | None = None) -> bool:
    """Check a password within the limits. Every refusal costs the same work as a wrong password."""
    from app.auth.passwords import verify_dummy, verify_password

    decision = begin_attempt(engine, user_id, client_address, device_cookie=device_cookie, session_authenticated=session_authenticated, now=now)
    with hash_slot():
        if not decision.check_password:
            verify_dummy(password)
            return False
        valid, new_hash = verify_password(password, decision.password_hash, decision.pepper_id)
    return finish_attempt(engine, decision, success=valid, new_hash=new_hash)


def expired_address_rows(session: Session, now: datetime, batch_size: int, *, lock: bool) -> list[str]:
    query = select(LoginThrottle.address_hash).where(LoginThrottle.last_failure_at < now - ADDRESS_RETENTION).order_by(LoginThrottle.last_failure_at).limit(batch_size)
    return session.exec(query.with_for_update(skip_locked=True) if lock else query).all()


def delete_address_rows(session: Session, digests: list[str]) -> None:
    if digests:
        session.execute(delete(LoginThrottle).where(LoginThrottle.address_hash.in_(digests)))
