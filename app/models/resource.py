# Resource request/response schemas
# Table model lives in app/tables.py
from typing import Literal, Optional

from pydantic import Field, model_validator
from sqlmodel import SQLModel

from app.models.users import UserPublicRead


class ResourceCapabilities(SQLModel):
    can_edit: bool
    can_manage_access: bool
    can_view_access: bool = Field(default=False, description="GET /resources/{mfid}/access would be accepted: a role on the record other than through public access, or platform administrator authority.")
    can_change_status: bool
    can_transfer: bool
    can_delete: bool = False
    can_delete_files: bool = Field(default=False, description="Datasets only. `DELETE /files/{file_id}` would be accepted for a file of this dataset: the caller may delete on the dataset and it is not pending deletion.")
    can_request_deletion: bool = Field(default=False, description="POST /resources/{mfid}/delete would be accepted: contributor for datasets and samples, the same as can_delete for projects and instruments, and no deletion request already active.")
    max_grant_role: Optional[Literal["viewer", "contributor", "editor", "admin"]] = None


class TransferOwnershipRequest(SQLModel):
    """Body for POST /resources/{mfid}/transfer_ownership: the new owner as one reference in any form."""
    new_owner_ref: Optional[str] = Field(default=None, description="The new owner: unique_id, linked ORCID, username or email.")
    new_owner: Optional[str] = Field(default=None, deprecated=True, description="Deprecated: use new_owner_ref.")

    @model_validator(mode="after")
    def one_reference(self):
        if self.new_owner_ref is None and self.new_owner is None:
            raise ValueError("Provide new_owner_ref")
        if None not in (self.new_owner_ref, self.new_owner) and self.new_owner_ref != self.new_owner:
            raise ValueError("Provide only new_owner_ref")
        self.new_owner = self.new_owner_ref or self.new_owner
        return self


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
