# Resource request/response schemas
# Table model lives in app/tables.py
from typing import Literal, Optional

from pydantic import Field, model_validator
from sqlmodel import SQLModel

from app.models.users import UserPublicRead


class ResourceCapabilities(SQLModel):
    """What the caller may do to this record, decided by the same rules as the routes. Returned on detail
    routes and GET /resources/{mfid}; null in list items. A field that does not apply to the record type is false."""
    can_edit: bool = Field(description="PATCH on the record would be accepted: editor or above.")
    can_manage_access: bool = Field(description="The caller may change who has access: grants on a dataset or sample (admin), members of a project (editor) or an instrument (admin).")
    can_view_access: bool = Field(default=False, description="GET /resources/{mfid}/access would be accepted: a role on the record other than through public access, or platform administrator authority.")
    can_change_status: bool = Field(description="Projects: PATCH status (editor). Instruments: POST /instruments/{instrument_ref}/status (admin). Always false for datasets and samples.")
    can_transfer: bool = Field(description="POST /resources/{mfid}/transfer_ownership would be accepted: owner.")
    can_delete: bool = Field(default=False, description="DELETE /resources/{mfid}?force=true would be accepted: owner, and for a project or instrument no datasets or samples still assigned.")
    can_delete_files: bool = Field(default=False, description="Datasets only. `DELETE /files/{file_id}` would be accepted for a file of this dataset: the caller may delete on the dataset and it is not pending deletion.")
    can_request_deletion: bool = Field(default=False, description="POST /resources/{mfid}/delete would be accepted: contributor for datasets and samples, the same as can_delete for projects and instruments, and no deletion request already active.")
    max_grant_role: Optional[Literal["viewer", "contributor", "editor", "admin"]] = Field(default=None, description="The highest role the caller may grant here, or null when they may grant none. Instruments never offer contributor.")


RECORD_ROLE_DESCRIPTION = (
    "The caller's effective role on the record, ignoring access that every user has because the record is public. "
    "Null when the caller holds none."
)


class ActiveDeletionRequestRead(SQLModel):
    """The deletion request pending or approved on a record, as embedded in the record's response."""
    id: int
    resource_mfid: str
    resource_id: str = Field(deprecated=True, description="Deprecated: use resource_mfid.")
    resource_name: Optional[str] = None
    requester_id: str
    reason: Optional[str] = None
    status: str
    request_time: Optional[str] = None
    review_time: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewer_notes: Optional[str] = None


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
