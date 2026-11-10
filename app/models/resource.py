# Resource request/response schemas
# Table model lives in app/tables.py
from typing import Literal, Optional

from pydantic import Field
from sqlmodel import SQLModel

from app.models.users import UserPublicRead


class ResourceCapabilities(SQLModel):
    can_edit: bool
    can_manage_access: bool
    can_view_access: bool = Field(default=False, description="GET /resources/{mfid}/access would be accepted: a role on the record other than through public access, or platform administrator authority.")
    can_change_status: bool
    can_transfer: bool
    can_delete: bool = False
    can_request_deletion: bool = Field(default=False, description="POST /resources/{mfid}/delete would be accepted: contributor for datasets and samples, the same as can_delete for projects and instruments, and no deletion request already active.")
    max_grant_role: Optional[Literal["viewer", "contributor", "editor", "admin"]] = None


class TransferOwnershipRequest(SQLModel):
    """Body for POST /resources/{mfid}/transfer_ownership.

    new_owner may be an ORCID, username, email, or (for a service account) an
    MFID — resolved by format via resolve_user_by_identifier. One field, not
    three, so there's nothing for a caller to leave conflicting.
    """
    new_owner: str


class TransferOwnershipRead(SQLModel):
    resource_id: str = Field(deprecated=True, description="Deprecated. Use resource_mfid.")
    resource_mfid: str
    previous_owner: Optional[UserPublicRead] = None
    new_owner: UserPublicRead


class ReassignProjectRequest(SQLModel):
    """Body for POST /resources/{mfid}/project."""
    project_id: Optional[str] = Field(
        default=None,
        description="Target project slug. May be combined with a matching project_mfid.",
    )
    project_mfid: Optional[str] = Field(
        default=None,
        description="Target project MFID. May be combined with a matching project_id.",
    )


class ReassignProjectRead(SQLModel):
    resource_id: str
    resource_mfid: str
    previous_project_id: Optional[str] = None
    previous_project_mfid: Optional[str] = None
    new_project_id: str
    new_project_mfid: str
