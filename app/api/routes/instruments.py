from typing import Annotated, List, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.instrument import Instrument
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["instruments"])
engine = get_engine()


@router.get("/instruments", response_model=List[Instrument])
def list_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    '''
    Returns a list of instruments filtered by any provided search terms that are attributes of the instrument Class.
    If no instruments matching the search criteria are found, an empty list is returned.
    Search parameters include these Instrument attributes (Note: not case sensitive, but otherwise must be exact match):
        other_id: str
        other_id_source: str
        instrument_name: str
        manufacturer: str
        model: str
        owner: str
        location: str
        description: str
        instrument_type: str
    '''
    access_group_name, ag_ids = auth_res
    query_params = request.query_params
    params_dict = dict(query_params)
    print(params_dict)
    statement = select(Instrument)
    for k, v in params_dict.items():
        if hasattr(Instrument, k):
            print(f'has attr {k=}')
            statement = statement.where(getattr(Instrument, k) == v.lower().strip())
        else:
            print(f'does not have attr {k=}')
    with Session(engine) as session:
        all_inst = session.exec(statement).all()

    return(all_inst)


@router.get("/instruments/{instrument_unique_id}", response_model=Union[None, Instrument])
def read_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument_unique_id: str):
    '''
    Returns an instrument with the provided ID if one exists. Otherwise returns None.
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        inst = session.exec(select(Instrument).where(Instrument.unique_id == instrument_unique_id)).one_or_none()
        return(inst)


@router.post("/instruments", response_model=Instrument)
def create_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument: Instrument):
    """
    Add a new instrument record to the database.

    Args:
        ag_ids: List of access group IDs the user belongs to; here only used to confirm user is in the database
        instrument: Instrument object to be added

    Returns:
        Added instrument object

    Raises:
        HTTPException: If required instrument fields are missing
    """
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if not instrument.instrument_name or not instrument.instrument_name.strip():
        missing_fields.append("instrument_name")
    if not instrument.owner or not instrument.owner.strip():
        missing_fields.append("owner")
    if not instrument.location or not instrument.location.strip():
        missing_fields.append("location")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required instrument information: {', '.join(missing_fields)}. Please provide all required fields: instrument_name, owner, and location."
        )

    instrument.instrument_name = instrument.instrument_name.lower()
    with Session(engine) as session:
        session.add(instrument)
        session.commit()
        session.refresh(instrument)
    return(instrument)
