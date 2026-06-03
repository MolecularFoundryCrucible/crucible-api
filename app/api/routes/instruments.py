from typing import Annotated, List, Optional, Union

import os
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import func, join
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_tz_isoformat
from app.models.core_models import Instrument, Resource, ScientificMetadataBinary
from app.models.instrument import InstrumentCreate, InstrumentRead, InstrumentResponse, InstrumentUpdate
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user
from app.utils.sql_utils import add_or_update
from app.utils.gcs_record_utils import write_record, instrument_record

router = APIRouter(tags=["instruments"])
engine = get_engine()

API_VERSION = os.getenv('API_VERSION', None)
API_VERSION_GITHASH = os.getenv("GITHASH", None)

@router.get("/instruments", response_model=PaginatedResponse[InstrumentResponse])
def list_instruments(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request,
                     include_metadata: bool = False,
                     limit: int = Query(default=100, ge=1, le=1000),
                     offset: int = Query(default=0, ge=0)):
    '''
    Returns a paginated list of instruments filtered by any provided query parameters that
    match Instrument attributes. Matching is case-insensitive exact match.
    Filterable fields: other_id, other_id_source, instrument_name, manufacturer,
    model, owner, location, description, instrument_type.
    '''
    query_params = dict(request.query_params)
    for k in ('include_metadata', 'limit', 'offset'):
        query_params.pop(k, None)
    filters = [getattr(Instrument, k) == v.lower().strip() for k, v in query_params.items() if hasattr(Instrument, k)]

    with Session(engine) as session:
        base = select(Instrument).join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
        for f in filters:
            base = base.where(f)

        total = session.exec(select(func.count()).select_from(base.subquery())).one()

        if include_metadata:
            statement = (
                select(Instrument, Resource, ScientificMetadataBinary)
                .join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)
                .join(ScientificMetadataBinary, ScientificMetadataBinary.unique_id == Instrument.unique_id, isouter=True)
            )
        else:
            statement = select(Instrument, Resource).join(Resource, Resource.mfid == Instrument.unique_id, isouter=True)

        for f in filters:
            statement = statement.where(f)

        rows = session.exec(statement.order_by(Resource.creation_time.desc()).offset(offset).limit(limit)).all()

        items = []
        for row in rows:
            if include_metadata:
                inst, ri, smb = row
                items.append(InstrumentResponse.model_validate({
                    **inst.model_dump(),
                    "creation_time": ri.creation_time if ri else None,
                    "modification_time": ri.modification_time if ri else None,
                    "scientific_metadata": smb.scientific_metadata if smb else None,
                }))
            else:
                inst, ri = row
                items.append(InstrumentResponse.model_validate({
                    **inst.model_dump(),
                    "creation_time": ri.creation_time if ri else None,
                    "modification_time": ri.modification_time if ri else None,
                }))
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


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
def create_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument: InstrumentCreate,
                      background_tasks: BackgroundTasks):
    '''
    Register a new instrument in the database.
    '''
    user_unique_id, _ = auth_res
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
        background_tasks.add_task(write_record, db_instrument.unique_id, instrument_record(db_instrument, ri), user_unique_id, "create")
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
    background_tasks: BackgroundTasks,
):
    '''
    Partially update an instrument record. Only provided fields are modified.
    '''
    user_unique_id, _ = auth_res
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
        background_tasks.add_task(write_record, instrument.unique_id, instrument_record(instrument, ri), user_unique_id, "update")
        return InstrumentResponse.model_validate({
            **instrument.model_dump(),
            "creation_time": ri.creation_time if ri else None,
            "modification_time": ri.modification_time if ri else None,
        })
