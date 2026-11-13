"""What the caller may do to a record: one policy per record type, applied by every route that returns capabilities."""
from dataclasses import dataclass
from typing import Optional

from sqlmodel import Session

from app.models.resource import ResourceCapabilities
from app.utils.permissions import Permission, Principal, Role


@dataclass(frozen=True)
class CapabilityPolicy:
    manage_access_floor: Role
    status_permission: Optional[Permission]
    request_deletion_with: Optional[Permission]
    holds_files: bool = False
    has_assigned_records: bool = False
    grant_roles: tuple = (Role.ADMIN, Role.EDITOR, Role.CONTRIBUTOR, Role.VIEWER)


POLICIES = {
    "dataset": CapabilityPolicy(Role.ADMIN, None, Permission.CREATE, holds_files=True),
    "sample": CapabilityPolicy(Role.ADMIN, None, Permission.CREATE),
    "project": CapabilityPolicy(Role.EDITOR, Permission.EDIT, None, has_assigned_records=True),
    "instrument": CapabilityPolicy(Role.ADMIN, Permission.DELETE, None, has_assigned_records=True,
                                   grant_roles=(Role.ADMIN, Role.EDITOR, Role.VIEWER)),
}


def record_capabilities(session: Session, principal: Principal, resource_type: str, mfid: str, *,
                        acl_rows=None, deletion_requested: Optional[bool] = None) -> ResourceCapabilities:
    from app.utils.auth_utils import resource_capabilities
    from app.utils.deletion_utils import has_deletion_blockers

    policy = POLICIES[resource_type]
    return resource_capabilities(
        session,
        principal,
        mfid,
        manage_access_floor=policy.manage_access_floor,
        status_permission=policy.status_permission,
        acl_rows=acl_rows,
        can_delete=(lambda: not has_deletion_blockers(session, resource_type, mfid)) if policy.has_assigned_records else True,
        request_deletion_with=policy.request_deletion_with,
        deletion_requested=deletion_requested,
        holds_files=policy.holds_files,
        grant_roles=policy.grant_roles,
    )
