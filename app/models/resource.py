# Resource request/response schemas
# Table model lives in core_models.py
from typing import Optional

from sqlmodel import SQLModel

from app.models.users import UserPublicRead


class TransferOwnershipRequest(SQLModel):
    """Body for POST /resources/{mfid}/transfer_ownership.

    new_owner may be an ORCID, username, email, or (for a service account) an
    MFID — resolved by format via resolve_user_by_identifier. One field, not
    three, so there's nothing for a caller to leave conflicting.
    """
    new_owner: str


class TransferOwnershipRead(SQLModel):
    resource_id: str
    previous_owner: Optional[UserPublicRead] = None
    new_owner: UserPublicRead


class ReassignProjectRequest(SQLModel):
    """Body for POST /resources/{mfid}/project."""
    project_id: str


class ReassignProjectRead(SQLModel):
    resource_id: str
    previous_project_id: Optional[str] = None
    new_project_id: str
