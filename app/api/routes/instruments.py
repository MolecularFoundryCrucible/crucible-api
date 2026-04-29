from typing import Annotated, List, Optional, Union

import os
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import join
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_tz_isoformat
from app.models.core_models import Instrument, Resource, ScientificMetadataBinary
from app.models.instrument import InstrumentCreate, InstrumentRead, InstrumentResponse, InstrumentUpdate
from app.utils.auth_utils import authorize_user
from app.utils.sql_utils import add_or_update

router = APIRouter(tags=["instruments"])
engine = get_engine()

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)

@router.get("/instruments", response_model=List[InstrumentResponse])
def list_instruments(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, include_metadata: bool = False):
    '''
    Returns a list of instruments filtered by any provided query parameters that
    match Instrument attributes. Matching is case-insensitive exact match.
    Filterable fields: other_id, other_id_source, instrument_name, manufacturer,
    model, owner, location, description, instrument_type.
    '''
    query_params = dict(request.query_params)
    query_params.pop('include_metadata', None)
    filters = [getattr(Instrument, k) == v.lower().strip() for k, v in query_params.items() if hasattr(Instrument, k)]

    with Session(engine) as session:
        if include_metadata:
            statement = (
                select(Instrument, Resource, ScientificMetadataBinary)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Instrument.unique_id, isouter=True)
            )
            for f in filters:
                statement = statement.where(f)
            results = session.exec(statement).all()
            return [
                InstrumentResponse.model_validate({
                    **inst.model_dump(),
                    "creation_time": ri.creation_time if ri else None,
                    "modification_time": ri.modification_time if ri else None,
                    "scientific_metadata": smb.scientific_metadata if smb else None,
                })
                for inst, ri, smb in results
            ]
        else:
            statement = select(Instrument, Resource).join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
            for f in filters:
                statement = statement.where(f)
            results = session.exec(statement).all()
            return [
                InstrumentResponse.model_validate({
                    **inst.model_dump(),
                    "creation_time": ri.creation_time if ri else None,
                    "modification_time": ri.modification_time if ri else None,
                })
                for inst, ri in results
            ]


@router.get("/instruments/{instrument_unique_id}", response_model=InstrumentResponse)
def read_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument_unique_id: str, include_metadata: bool = False):
    '''
    Returns the instrument with the provided unique ID, or 404 if not found.
    '''
    with Session(engine) as session:
        if include_metadata:
            result = session.exec(
                select(Instrument, Resource, ScientificMetadataBinary)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Instrument.unique_id, isouter=True)
                .where(Instrument.unique_id == instrument_unique_id)
            ).one_or_none()
            if result is None:
                raise HTTPException(status_code=404, detail="Instrument not found")
            inst, ri, smb = result
            return InstrumentResponse.model_validate({
                **inst.model_dump(),
                "creation_time": ri.creation_time if ri else None,
                "modification_time": ri.modification_time if ri else None,
                "scientific_metadata": smb.scientific_metadata if smb else None,
            })
        else:
            result = session.exec(
                select(Instrument, Resource)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .where(Instrument.unique_id == instrument_unique_id)
            ).one_or_none()
            if result is None:
                raise HTTPException(status_code=404, detail="Instrument not found")
            inst, ri = result
            return InstrumentResponse.model_validate({
                **inst.model_dump(),
                "creation_time": ri.creation_time if ri else None,
                "modification_time": ri.modification_time if ri else None,
            })


@router.post("/instruments", response_model=InstrumentResponse)
def create_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument: InstrumentCreate):
    '''
    Register a new instrument in the database.
    '''
    with Session(engine) as session:
        db_instrument = Instrument(**instrument.model_dump())
        db_instrument.instrument_name = db_instrument.instrument_name.lower()
        session.add(db_instrument)
        now = get_tz_isoformat()
        ri = add_or_update(session, Resource,
                           where=(Resource.mfid == db_instrument.unique_id),
                           mfid=db_instrument.unique_id,
                           resource_type="instrument", 
                           created_with_api_version = API_VERSION,
                           api_version_githash = API_VERSION_GITHASH)
        ri.creation_time = now
        ri.modification_time = now
        session.commit()
        session.refresh(db_instrument)
        return InstrumentResponse.model_validate({
            **db_instrument.model_dump(),
            "creation_time": ri.creation_time,
            "modification_time": ri.modification_time,
        })


@router.patch("/instruments/{instrument_unique_id}", response_model=InstrumentResponse)
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

        ri = session.exec(select(Resource).where(Resource.mfid == instrument_unique_id)).one_or_none()
        if ri:
            ri.modification_time = get_tz_isoformat()

        session.add(instrument)
        session.commit()
        session.refresh(instrument)
        return InstrumentResponse.model_validate({
            **instrument.model_dump(),
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })
