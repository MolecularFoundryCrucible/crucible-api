from typing import Optional
from pydantic import field_validator
from sqlmodel import SQLModel


class InstrumentCreate(SQLModel):
    '''Fields required to register a new instrument.'''
    instrument_id: str
    instrument_name: str
    owner: str
    location: str
    other_id: Optional[str] = None
    other_id_source: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    instrument_type: Optional[str] = None

    @field_validator("instrument_id")
    @classmethod
    def check_instrument_id(cls, v):
        from app.utils.instrument_utils import validate_instrument_id_format
        return validate_instrument_id_format(v)


class InstrumentUpdate(SQLModel):
    '''Partial update schema for Instrument. Only provided fields are modified.'''
    instrument_id: Optional[str] = None
    instrument_name: Optional[str] = None
    owner: Optional[str] = None
    location: Optional[str] = None
    other_id: Optional[str] = None
    other_id_source: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    instrument_type: Optional[str] = None

    @field_validator("instrument_id")
    @classmethod
    def check_instrument_id(cls, v):
        from app.utils.instrument_utils import validate_instrument_id_format
        return validate_instrument_id_format(v) if v is not None else v


class InstrumentRead(SQLModel):
    '''Response model for Instrument. Includes server-generated fields.'''
    # id: int
    unique_id: Optional[str] = None
    instrument_id: Optional[str] = None
    instrument_name: str
    owner: str
    location: str
    other_id: Optional[str] = None
    other_id_source: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    instrument_type: Optional[str] = None


class InstrumentResponse(InstrumentRead):
    '''InstrumentRead extended with Resource timestamps.'''
    resource_type: Optional[str] = "instrument"
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None
    scientific_metadata: Optional[dict] = None
    status: Optional[str] = None
