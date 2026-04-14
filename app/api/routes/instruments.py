from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Instrument
from app.models.instrument import InstrumentCreate, InstrumentRead, InstrumentUpdate
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["instruments"])
engine = get_engine()


@router.get("/instruments", response_model=List[InstrumentRead])
def list_instruments(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    '''
    Returns a list of instruments filtered by any provided query parameters that
    match Instrument attributes. Matching is case-insensitive exact match.
    Filterable fields: other_id, other_id_source, instrument_name, manufacturer,
    model, owner, location, description, instrument_type.
    '''
    query_params = dict(request.query_params)
    statement = select(Instrument)
    for k, v in query_params.items():
        if hasattr(Instrument, k):
            statement = statement.where(getattr(Instrument, k) == v.lower().strip())

    with Session(engine) as session:
        return session.exec(statement).all()


@router.get("/instruments/{instrument_unique_id}", response_model=Union[None, InstrumentRead])
def read_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument_unique_id: str):
    '''
    Returns the instrument with the provided unique ID, or None if not found.
    '''
    with Session(engine) as session:
        return session.exec(
            select(Instrument).where(Instrument.unique_id == instrument_unique_id)
        ).one_or_none()


@router.post("/instruments", response_model=InstrumentRead)
def create_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument: InstrumentCreate):
    '''
    Register a new instrument in the database.
    '''
    with Session(engine) as session:
        db_instrument = Instrument(**instrument.model_dump())
        db_instrument.instrument_name = db_instrument.instrument_name.lower()
        session.add(db_instrument)
        session.commit()
        session.refresh(db_instrument)
        return db_instrument


@router.patch("/instruments/{instrument_unique_id}", response_model=InstrumentRead)
def update_instrument(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    instrument_unique_id: str,
    instrument_update: InstrumentUpdate,
):
    '''
    Partially update an instrument record. Only provided fields are modified.
    '''
    with Session(engine) as session:
        instrument = session.exec(
            select(Instrument).where(Instrument.unique_id == instrument_unique_id)
        ).one_or_none()

        if instrument is None:
            raise HTTPException(status_code=404, detail="Instrument not found")

        update_data = instrument_update.model_dump(exclude_unset=True)
        if "instrument_name" in update_data:
            update_data["instrument_name"] = update_data["instrument_name"].lower()

        for key, value in update_data.items():
            setattr(instrument, key, value)

        session.add(instrument)
        session.commit()
        session.refresh(instrument)
        return instrument
