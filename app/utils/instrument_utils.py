import re

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

from app.models.core_models import Instrument

_INSTRUMENT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$')


def validate_instrument_id_format(instrument_id: str) -> str:
    if not isinstance(instrument_id, str) or not _INSTRUMENT_ID_RE.match(instrument_id):
        raise ValueError(
            "instrument_id must be 2-64 chars, start with a letter or digit, "
            "and contain only letters, digits, underscores, hyphens"
        )
    return instrument_id


def assert_instrument_id_available(session, instrument_id: str, exclude_unique_id: str = None) -> None:
    """Raise 409 if instrument_id is already taken by a different instrument. Case-insensitive."""
    q = select(Instrument).where(func.lower(Instrument.instrument_id) == instrument_id.lower())
    if exclude_unique_id:
        q = q.where(Instrument.unique_id != exclude_unique_id)
    if session.exec(q).one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"instrument_id '{instrument_id}' already exists.")
