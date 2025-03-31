from typing import Optional
from sqlmodel import Field, SQLModel
from mfid import mfid


def get_mfid_value():
    x = mfid()[0]
    return(x)


class Instrument(SQLModel, table = True):
    id: int = Field(default=None, primary_key=True)
    unique_id: Optional[str] = Field(default_factory=get_mfid_value)
    other_id: Optional[str]
    other_id_source: Optional[str]
    instrument_name: str
    manufacturer: Optional[str]
    model: Optional[str]
    owner: str
    location: str
    description: Optional[str]
    instrument_type: Optional[str]
