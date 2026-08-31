from typing import Any, List, Optional
from pydantic import ConfigDict, Field, field_validator, model_validator
from sqlmodel import SQLModel

from app.general_utils import blank_to_none, is_valid_mfid
from app.models.datasets import DatasetRead
from app.models.links import LinkedResource


class SampleCreate(SQLModel):
    model_config = ConfigDict(extra="forbid")

    '''Fields accepted when registering a new sample.

    Provide unique_id to register a sample under a pre-assigned ID (must be a
    valid mfid, e.g. one generated client-side by an instrument using the
    same mfid library); omit it to create a new one with an auto-generated MFID.
    '''
    unique_id: Optional[str] = None

    @field_validator("unique_id")
    @classmethod
    def check_unique_id(cls, v):
        if v is not None and not is_valid_mfid(v):
            raise ValueError("unique_id must be a valid mfid: 26-character lowercase Crockford's Base32")
        return v
    sample_name: str
    sample_type: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None
    public: bool = False
    owner_orcid: Optional[str] = Field(
        default=None,
        deprecated=True,
        description="Deprecated canonical owner identity (human ORCID or service-account MFID). Use owner.",
    )
    owner: Optional[str] = Field(
        default=None,
        description="Owner reference: ORCID, service-account MFID, username, or email. Defaults to the authenticated identity.",
    )

    @field_validator("project_id", "owner_orcid", "owner", mode="before", check_fields=False)
    @classmethod
    def blank_identifiers_to_none(cls, v):
        """Uploaders that send "" instead of omitting the field would otherwise
        attach the resource to a nameless project. See general_utils."""
        return blank_to_none(v)

    @model_validator(mode="after")
    def validate_owner_reference(self):
        values = self.model_dump()
        if values.get("owner") is not None and values.get("owner_orcid") is not None:
            raise ValueError("Provide owner or owner_orcid, not both")
        return self

class SampleUpdate(SQLModel):
    '''Partial update schema for Sample. Only provided fields are modified.

    No project_id — use POST /resources/{mfid}/project.
    '''
    model_config = ConfigDict(extra="forbid")

    sample_name: Optional[str] = None
    sample_type: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None
    public: Optional[bool] = None

class SampleRead(SQLModel):
    '''Response model for Sample. Includes server-generated fields.'''
    # id: int
    unique_id: str
    sample_name: str
    sample_type: Optional[str] = None
    owner_orcid: Optional[str] = None
    project_id: Optional[str] = None
    description: Optional[str] = None
    timestamp: Optional[str] = None
    public: bool = False


class SampleResponse(SampleRead):
    '''SampleRead extended with Resource timestamps, associated datasets, and deletion status.'''
    resource_type: Optional[str] = "sample"
    creation_time: Optional[str] = None
    modification_time: Optional[str] = None
    scientific_metadata: Optional[dict] = None
    datasets: Optional[List[DatasetRead]] = None
    deletion_request: Optional[dict] = None
    links: Optional[List[LinkedResource]] = None
    owner: Optional[Any] = None  # UserPublicRead when include_owner=true
