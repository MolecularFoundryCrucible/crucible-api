from typing import Optional
from sqlmodel import Field, SQLModel


# class DatasetInstrumentLink(SQLModel, table=True):
#     instrument_id: Optional[int] = Field(
#         default=None, foreign_key="instrument.id", primary_key=True,
#         index=True
#     )
#     dataset_id: Optional[int] = Field(
#         default=None, foreign_key="dataset.id", primary_key=True
#     )


class Instrument(SQLModel, table = True):
    id: int = Field(default=None, primary_key=True)
    unique_id: str 
    other_id: str
    other_id_source: str
    instrument_name: str
    manufacturer: Optional[str]
    model: Optional[str]
    owner: str
    location: str
    description: Optional[str]
    instrument_type: Optional[str]
