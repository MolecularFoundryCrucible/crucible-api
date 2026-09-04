from typing import Optional
from pydantic import ConfigDict, Field, field_validator, model_validator
from sqlmodel import SQLModel

from app.general_utils import blank_to_none
from app.models.resource import ResourceCapabilities
from app.models.users import UserPublicRead


class InstrumentCreate(SQLModel):
    '''Fields required to register a new instrument.'''
    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    instrument_name: str
    owner: Optional[str] = Field(
        default=None,
        description="Owner reference: ORCID, service-account MFID, username, or email. Defaults to the authenticated identity.",
    )
    owner_orcid: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated canonical owner identity (human ORCID or service-account MFID). Use owner.",
    )
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

    @field_validator("owner", "owner_orcid", mode="before")
    @classmethod
    def blank_owner_to_none(cls, v):
        return blank_to_none(v)

    @model_validator(mode="after")
    def validate_owner_reference(self):
        if self.owner is not None and self.owner_orcid is not None:
            raise ValueError("Provide owner or owner_orcid, not both")
        return self


class InstrumentUpdate(SQLModel):
    '''Partial update schema for Instrument. Only provided fields are modified.'''
    model_config = ConfigDict(extra="forbid")

    instrument_id: str = None
    instrument_name: str = None
    location: str = None
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


class InstrumentRead(SQLModel):
    '''Response model for Instrument. Includes server-generated fields.'''
    # id: int
    unique_id: str
    instrument_id: Optional[str] = None
    instrument_name: str
    owner_orcid: str = Field(description="Canonical owner identity: human ORCID or service-account MFID.")
    owner: Optional[UserPublicRead] = None
    location: str
    other_id: Optional[str] = None
    other_id_source: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    description: Optional[str] = None
    instrument_type: Optional[str] = None


class InstrumentReferenceRead(SQLModel):
    unique_id: str
    instrument_id: Optional[str] = None
    instrument_name: str


class InstrumentResponse(InstrumentRead):
    '''InstrumentRead extended with Resource timestamps.'''
    resource_type: Optional[str] = "instrument"
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None
    scientific_metadata: Optional[dict] = None
    status: Optional[str] = None
    capabilities: Optional[ResourceCapabilities] = None
