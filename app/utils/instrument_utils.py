import re

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select

from app.models.core_models import Instrument, Project

_INSTRUMENT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{2,24}$')


def validate_instrument_id_format(instrument_id: str) -> str:
    if not isinstance(instrument_id, str) or not _INSTRUMENT_ID_RE.fullmatch(instrument_id):
        raise ValueError(
            "instrument_id must be 3-25 chars, start with a letter or digit, "
            "and contain only letters, digits, underscores, hyphens"
        )
    return instrument_id


def assert_instrument_id_available(session, instrument_id: str, exclude_unique_id: str = None) -> None:
    """Raise 409 if instrument_id is already taken by a different instrument,
    or collides with an existing project_id — the two share no namespace,
    but nothing stopped them coincidentally colliding otherwise. Case-insensitive."""
    q = select(Instrument).where(func.lower(Instrument.instrument_id) == instrument_id.lower())
    if exclude_unique_id:
        q = q.where(Instrument.unique_id != exclude_unique_id)
    if session.exec(q).one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"instrument_id '{instrument_id}' already exists.")
    if session.exec(select(Project).where(func.lower(Project.project_id) == instrument_id.lower())).one_or_none() is not None:
        raise HTTPException(status_code=409, detail=f"instrument_id '{instrument_id}' collides with an existing project_id.")


def resolve_instrument_id_to_mfid(instrument_id: str, session) -> "str | None":
    """Resolve an instrument_id slug to its Instrument.unique_id (MFID).
    None if instrument_id does not name a real instrument."""
    instrument = session.exec(select(Instrument).where(Instrument.instrument_id == instrument_id)).one_or_none()
    return instrument.unique_id if instrument else None


def resolve_instrument_mfids_to_ids(mfids, session) -> dict:
    """Batch mfid -> instrument_id, for response serialization. One query
    regardless of how many rows are being resolved."""
    mfid_set = {m for m in mfids if m}
    if not mfid_set:
        return {}
    rows = session.exec(
        select(Instrument.unique_id, Instrument.instrument_id).where(Instrument.unique_id.in_(mfid_set))
    ).all()
    return dict(rows)
