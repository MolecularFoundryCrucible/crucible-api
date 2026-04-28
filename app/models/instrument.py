from typing import Optional
from sqlmodel import SQLModel


class InstrumentCreate(SQLModel):
    '''Fields required to register a new instrument.'''
    instrument_name: str
    owner: str
    location: str
    other_id: Optional[str] = None
    other_id_source: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    instrument_type: Optional[str] = None


class InstrumentUpdate(SQLModel):
    '''Partial update schema for Instrument. Only provided fields are modified.'''
    instrument_name: Optional[str] = None
    owner: Optional[str] = None
    location: Optional[str] = None
    other_id: Optional[str] = None
    other_id_source: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    instrument_type: Optional[str] = None


class InstrumentRead(SQLModel):
    '''Response model for Instrument. Includes server-generated fields.'''
    # id: int
    unique_id: Optional[str] = None
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
