"""Device sign-in (RFC 8628) for programs on machines without a browser: the program shows a short
code, and the person approves it in a browser elsewhere after a fresh sign-in.

The device code is stored as a plain hash; the user code, 8 characters from a 20-letter alphabet
(about 34 bits), as a keyed hash, so a copy of the database cannot reverse it. Both last 10 minutes
rather than RFC 8628's 30-minute example, to shorten the window for phishing (RFC 8628 5.4, RFC
10027). Wrong user codes count against the per-address attempt limit."""
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.config import get_secret
from app.tables import DeviceAuthorization

USER_CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ"
USER_CODE_LENGTH = 8
DEVICE_LIFETIME = timedelta(minutes=10)
POLL_INTERVAL = 5
SLOW_DOWN_STEP = 5
INTERNAL_CLIENT_ID = "crucible-device-approval"


def _now(value: datetime | None) -> datetime:
    return value if value is not None else datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def device_code_hash(device_code: str) -> str:
    return hashlib.sha256(device_code.encode("utf-8")).hexdigest()


def normalized_user_code(text: str | None) -> str | None:
    """What a person typed, as a user code: case, spaces, dashes and other marks do not matter."""
    if not text or len(text) > 64:
        return None
    letters = "".join(character for character in text.upper() if character in USER_CODE_ALPHABET)
    return letters if len(letters) == USER_CODE_LENGTH else None


def user_code_hash(code: str) -> str:
    return hmac.new(get_secret("CRUCIBLE_SECRET_KEY").encode("utf-8"), b"device-user-code\0" + code.encode("ascii"), hashlib.sha256).hexdigest()


def new_user_code() -> str:
    letters = "".join(secrets.choice(USER_CODE_ALPHABET) for _ in range(USER_CODE_LENGTH))
    return f"{letters[:4]}-{letters[4:]}"


def create_device_authorization(engine, client_id: str, *, requested_from: str | None = None, now: datetime | None = None) -> tuple[str, str]:
    """A new device sign-in for this client: its device code and its user code, each shown once."""
    current = _now(now)
    for _ in range(5):
        device_code, user_code = secrets.token_urlsafe(48), new_user_code()
        with Session(engine) as session:
            if session.exec(select(DeviceAuthorization.device_code_hash).where(DeviceAuthorization.user_code_hash == user_code_hash(normalized_user_code(user_code)))).first() is None:
                session.add(DeviceAuthorization(
                    device_code_hash=device_code_hash(device_code), user_code_hash=user_code_hash(normalized_user_code(user_code)), client_id=client_id,
                    created_at=current, expires_at=current + DEVICE_LIFETIME, interval=POLL_INTERVAL, requested_from=requested_from,
                ))
                session.commit()
                return device_code, user_code
    raise RuntimeError("could not find a free user code")


@dataclass(frozen=True)
class PendingDevice:
    device_hash: str
    client_id: str
    created_at: datetime
    user_code: str
    requested_from: str | None = None


def pending_device(engine, user_code: str | None, *, now: datetime | None = None) -> PendingDevice | None:
    """The device sign-in this code names, if it is still waiting for a decision."""
    code = normalized_user_code(user_code)
    if code is None:
        return None
    current = _now(now)
    with Session(engine) as session:
        row = session.exec(select(DeviceAuthorization).where(DeviceAuthorization.user_code_hash == user_code_hash(code))).one_or_none()
        if row is None or row.approved_at is not None or row.denied_at is not None or row.consumed_at is not None or current >= _aware(row.expires_at):
            return None
        return PendingDevice(row.device_code_hash, row.client_id, _aware(row.created_at), f"{code[:4]}-{code[4:]}")


def device_by_hash(engine, device_hash: str, *, now: datetime | None = None) -> PendingDevice | None:
    current = _now(now)
    with Session(engine) as session:
        row = session.get(DeviceAuthorization, device_hash)
        if row is None or row.approved_at is not None or row.denied_at is not None or row.consumed_at is not None or current >= _aware(row.expires_at):
            return None
        return PendingDevice(row.device_code_hash, row.client_id, _aware(row.created_at), "", row.requested_from)


def _waiting(device_hash: str, current: datetime) -> tuple:
    return (
        DeviceAuthorization.device_code_hash == device_hash, DeviceAuthorization.approved_at.is_(None), DeviceAuthorization.denied_at.is_(None),
        DeviceAuthorization.consumed_at.is_(None), DeviceAuthorization.expires_at > current,
    )


def record_signed_in(engine, device_hash: str, *, user_id: int, auth_time: datetime, now: datetime | None = None) -> bool:
    """Note who signed in to decide on this device sign-in. Only that person's decision is then
    accepted, so the signed form that carries it cannot name anyone else."""
    with Session(engine) as session, session.begin():
        row = session.execute(update(DeviceAuthorization).where(*_waiting(device_hash, _now(now)))
                              .values(user_id=user_id, auth_time=auth_time).execution_options(synchronize_session=False).returning(DeviceAuthorization.device_code_hash)).first()
    return row is not None


def decide(engine, device_hash: str, *, user_id: int, approve: bool, now: datetime | None = None) -> bool:
    """Record the decision of the person who signed in for it, once, while the device sign-in is still waiting."""
    current = _now(now)
    with Session(engine) as session, session.begin():
        row = session.execute(update(DeviceAuthorization).where(*_waiting(device_hash, current), DeviceAuthorization.user_id == user_id)
                              .values(**({"approved_at": current} if approve else {"denied_at": current}))
                              .execution_options(synchronize_session=False).returning(DeviceAuthorization.device_code_hash)).first()
    return row is not None


def remove_expired(session: Session, now: datetime, batch_size: int, *, commit: bool) -> int:
    from sqlalchemy import delete

    query = select(DeviceAuthorization.device_code_hash).where(DeviceAuthorization.expires_at <= now - timedelta(days=1)).order_by(DeviceAuthorization.expires_at).limit(batch_size)
    hashes = session.exec(query.with_for_update(skip_locked=True) if commit else query).all()
    if commit and hashes:
        session.execute(delete(DeviceAuthorization).where(DeviceAuthorization.device_code_hash.in_(hashes)))
    return len(hashes)
