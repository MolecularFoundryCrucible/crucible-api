from enum import Enum
from typing import List, Optional
from pydantic import ConfigDict, Field, field_validator, model_validator
from sqlmodel import SQLModel

from app.general_utils import blank_to_none, fold_owner_ref
from app.models.access import AccessPermission
from app.models.access_groups import AccessGroupMemberRead
from app.models.resource import ActiveDeletionRequestRead, ResourceCapabilities
from app.models.timestamps import UtcDatetime
from app.models.users import UserPublicRead


class InstrumentMembershipRole(str, Enum):
    VIEWER = "viewer"
    EDITOR = "editor"
    ADMIN = "admin"


class InstrumentCreate(SQLModel):
    '''Fields required to register a new instrument.'''
    model_config = ConfigDict(extra="forbid")

    instrument_id: str
    instrument_name: str
    owner_ref: Optional[str] = Field(
        default=None,
        exclude=True,
        description="Owner: unique_id, linked ORCID, username or email. Defaults to the authenticated identity.",
    )
    owner: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated: use owner_ref.",
    )
    owner_orcid: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated canonical owner identity (human ORCID or service-account MFID). Use owner_ref.",
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

    @field_validator("owner", "owner_orcid", "owner_ref", mode="before")
    @classmethod
    def blank_owner_to_none(cls, v):
        return blank_to_none(v)

    @model_validator(mode="after")
    def validate_owner_reference(self):
        return fold_owner_ref(self)


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
    owner_orcid: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use owner.unique_id.")
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
    creation_time: Optional[UtcDatetime] = None
    modification_time: Optional[UtcDatetime] = None
    last_activity_at: Optional[UtcDatetime] = None
    scientific_metadata: Optional[dict] = None
    status: Optional[str] = None
    capabilities: Optional[ResourceCapabilities] = None
    deletion_request: Optional[ActiveDeletionRequestRead] = None
    members: Optional[List[AccessGroupMemberRead]] = Field(
        default=None,
        description="The instrument's members, highest role first. Null unless requested and visible to the caller.",
    )
    role: Optional[AccessPermission] = Field(
        default=None,
        description=(
            "The caller's membership role on the instrument. Null when the caller holds none; "
            "bound service accounts are operators, not members."
        ),
    )
