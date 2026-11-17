"""Small helpers the sign-in modules share."""
import hashlib
from datetime import datetime, timezone


def now_or(value: datetime | None) -> datetime:
    """`value`, or the current time in UTC when it is None, so callers can pin the clock."""
    return value if value is not None else datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    """A timestamp read back without its zone (SQLite drops it) taken as UTC; None stays None."""
    return value if value is None or value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def sha256_hex(value: str) -> str:
    """How tokens, codes and keys are stored: the hex SHA-256 of their text."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
