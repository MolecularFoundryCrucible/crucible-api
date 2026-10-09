from sqlmodel import Session, select

from app.models.access import AccessGrantRead
from app.models.core_models import AccessGroup, Instrument, Project, ResourceACL, User
from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType, Principal, Role, effective_role


def access_grant_read(session, access_group: AccessGroup, permission: str) -> AccessGrantRead:
    group_name = access_group.group_name
    group_type = access_group.group_type
    if group_type == AccessGroupType.SYSTEM.value and group_name == PUBLIC_GROUP_NAME:
        return AccessGrantRead(
            principal_id=PUBLIC_GROUP_NAME,
            principal_type="public",
            permission=permission,
            display_name="All authenticated users",
        )
    if group_type == AccessGroupType.SYSTEM.value:
        return AccessGrantRead(
            principal_id=group_name,
            principal_type="system",
            permission=permission,
            display_name=group_name,
        )
    if group_type == AccessGroupType.IDENTITY.value:
        user = session.exec(select(User).where(User.unique_id == group_name)).one_or_none()
    else:
        user = None
    if user:
        display_name = f"{user.first_name} {user.last_name}".strip() or None
        return AccessGrantRead(
            principal_id=user.unique_id,
            principal_type="service_account" if user.is_service_account else "user",
            permission=permission,
            slug=user.username,
            display_name=display_name,
        )
    if group_type == AccessGroupType.PROJECT.value:
        project = session.exec(select(Project).where(Project.unique_id == group_name)).one_or_none()
    else:
        project = None
    if project:
        return AccessGrantRead(
            principal_id=project.unique_id,
            principal_type="project",
            permission=permission,
            slug=project.project_id,
            display_name=project.title or project.project_id,
        )
    if group_type == AccessGroupType.INSTRUMENT.value:
        instrument = session.exec(select(Instrument).where(Instrument.unique_id == group_name)).one_or_none()
    else:
        instrument = None
    if instrument:
        return AccessGrantRead(
            principal_id=instrument.unique_id,
            principal_type="instrument",
            permission=permission,
            slug=instrument.instrument_id,
            display_name=instrument.instrument_name,
        )
    return AccessGrantRead(
        principal_id=group_name,
        principal_type="unknown",
        permission=permission,
    )


def load_member_grants(session: Session, mfid: str) -> list[AccessGrantRead]:
    """Grants held by people and projects on a resource, excluding the
    resource's own group, public access, and system groups."""
    rows = session.exec(
        select(ResourceACL, AccessGroup)
        .join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id)
        .where(
            ResourceACL.mfid == mfid,
            AccessGroup.group_name != mfid,
            AccessGroup.group_type.in_((AccessGroupType.IDENTITY.value, AccessGroupType.PROJECT.value)),
        )
    ).all()
    grants = [access_grant_read(session, ag, Role.from_db(acl.permission).db_value) for acl, ag in rows]
    return sorted(grants, key=lambda grant: (-Role.from_db(grant.permission.value), grant.principal_id))


def record_role(principal: Principal, acl_rows) -> Role | None:
    """The principal's effective role on a resource record, ignoring public
    access, which is visibility rather than a role."""
    public_ids = {m.id for m in principal.groups if m.group_name == PUBLIC_GROUP_NAME}
    return effective_role(principal, [(gid, role) for gid, role in acl_rows if gid not in public_ids])


def may_view_members(session: Session, principal: Principal, mfid: str) -> bool:
    """Platform administrators and callers holding any role on the resource
    other than through public access."""
    if principal.is_platform_admin:
        return True
    from app.utils.auth_utils import fetch_acl_rows
    return record_role(principal, fetch_acl_rows(session, mfid)) is not None
