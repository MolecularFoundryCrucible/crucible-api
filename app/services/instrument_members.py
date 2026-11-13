from typing import Optional

from fastapi import HTTPException
from sqlalchemy import delete
from sqlmodel import Session, select

from app.models.access import AccessGrantRead, EntryCapabilities
from app.models.access_groups import AccessGroupMemberRead
from app.tables import AccessGroup, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, Principal, Role, effective_role

MEMBERS_SUFFIX = ":members"
MEMBER_ROLES = (Role.VIEWER, Role.EDITOR, Role.ADMIN)


def members_group_name(instrument_mfid: str) -> str:
    return f"{instrument_mfid}{MEMBERS_SUFFIX}"


def instrument_of_members_group(group_name: str) -> Optional[str]:
    return group_name[:-len(MEMBERS_SUFFIX)] if group_name.endswith(MEMBERS_SUFFIX) else None


def members_group(session: Session, instrument_mfid: str) -> Optional[AccessGroup]:
    return session.exec(select(AccessGroup).where(
        AccessGroup.group_name == members_group_name(instrument_mfid),
        AccessGroup.group_type == AccessGroupType.INSTRUMENT_MEMBER.value,
    )).one_or_none()


def require_members_group(session: Session, instrument_mfid: str) -> AccessGroup:
    group = members_group(session, instrument_mfid)
    if group is None:
        raise HTTPException(status_code=409, detail=f"Instrument '{instrument_mfid}' has no members group")
    return group


def ensure_members_group(session: Session, instrument_mfid: str) -> AccessGroup:
    """The instrument's members group, created empty if an instrument written by an earlier release lacks one."""
    group = members_group(session, instrument_mfid)
    if group is None:
        group = AccessGroup(group_name=members_group_name(instrument_mfid), group_type=AccessGroupType.INSTRUMENT_MEMBER.value)
        session.add(group)
        session.flush()
    return group


def create_members_group(session: Session, instrument_mfid: str, owner: User) -> AccessGroup:
    """The members group of a new instrument, with `owner` as its owner and the group's owner grant on the record."""
    from app.utils.sql_utils import sql_create_resource_acl

    group = AccessGroup(group_name=members_group_name(instrument_mfid), group_type=AccessGroupType.INSTRUMENT_MEMBER.value)
    session.add(group)
    session.flush()
    session.add(UserAccessGroupLink(user_id=owner.id, accessgroup_id=group.id, role=Role.OWNER.db_value))
    sql_create_resource_acl(group, instrument_mfid, read=True, write=True, session=session, permission=Role.OWNER)
    return group


def grant_dataset(session: Session, dataset_mfid: str, instrument_mfid: str) -> None:
    """Let the instrument's members read the dataset."""
    from app.utils.sql_utils import sql_create_resource_acl

    sql_create_resource_acl(ensure_members_group(session, instrument_mfid), dataset_mfid,
                            read=True, write=False, session=session, permission=Role.VIEWER)


def revoke_dataset(session: Session, dataset_mfid: str, instrument_mfid: str) -> None:
    group = members_group(session, instrument_mfid)
    if group is not None:
        session.execute(delete(ResourceACL).where(ResourceACL.mfid == dataset_mfid, ResourceACL.accessgroup_id == group.id))


def member_link(session: Session, group: AccessGroup, user: User) -> Optional[UserAccessGroupLink]:
    return session.exec(select(UserAccessGroupLink).where(
        UserAccessGroupLink.user_id == user.id, UserAccessGroupLink.accessgroup_id == group.id,
    )).one_or_none()


def _member_rows(session: Session, instrument_mfid: str) -> list[tuple[User, Role]]:
    rows = session.exec(
        select(User, UserAccessGroupLink.role)
        .join(UserAccessGroupLink, UserAccessGroupLink.user_id == User.id)
        .join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id)
        .where(
            AccessGroup.group_name == members_group_name(instrument_mfid),
            AccessGroup.group_type == AccessGroupType.INSTRUMENT_MEMBER.value,
        )
    ).all()
    members = [(user, Role.from_db(role)) for user, role in rows]
    return sorted(members, key=lambda member: (-member[1], member[0].unique_id))


def _members_with_capabilities(session: Session, principal: Principal, instrument_mfid: str):
    from app.utils.auth_utils import entry_capabilities, fetch_acl_rows

    actor = effective_role(principal, fetch_acl_rows(session, instrument_mfid))
    for user, role in _member_rows(session, instrument_mfid):
        yield user, role, EntryCapabilities(**entry_capabilities(
            principal, actor, role, floor=Role.ADMIN, is_self=principal.is_user(user), roles=MEMBER_ROLES))


def load_members(session: Session, principal: Principal, instrument_mfid: str) -> list[AccessGroupMemberRead]:
    """The instrument's members, highest role first, with what `principal` may do to each."""
    return [
        AccessGroupMemberRead.model_validate(user, update={"role": role.db_value, "capabilities": capabilities})
        for user, role, capabilities in _members_with_capabilities(session, principal, instrument_mfid)
    ]


def load_member_grants(session: Session, principal: Principal, instrument_mfid: str) -> list[AccessGrantRead]:
    """The members in the shape of `members` on instrument responses."""
    return [
        AccessGrantRead(
            principal_id=user.unique_id,
            principal_type="user",
            permission=role.db_value,
            slug=user.username,
            display_name=f"{user.first_name} {user.last_name}".strip() or None,
            capabilities=capabilities,
        )
        for user, role, capabilities in _members_with_capabilities(session, principal, instrument_mfid)
    ]


def member_filter(user_id: str, roles):
    """Instruments where `user_id` is a member at one of `roles`, read from the membership table
    rather than the caller's cached groups, so a new instrument is listed at once."""
    from sqlalchemy import exists

    from app.tables import Instrument

    return (
        exists()
        .where(
            AccessGroup.group_name == Instrument.unique_id + MEMBERS_SUFFIX,
            AccessGroup.group_type == AccessGroupType.INSTRUMENT_MEMBER.value,
            UserAccessGroupLink.accessgroup_id == AccessGroup.id,
            UserAccessGroupLink.user_id == User.id,
            User.unique_id == user_id,
            UserAccessGroupLink.role.in_([role.db_value for role in roles]),
        )
        .select_from(AccessGroup, UserAccessGroupLink, User)
        .correlate(Instrument)
    )


def current_owner(session: Session, instrument_mfid: str) -> Optional[User]:
    group = members_group(session, instrument_mfid)
    if group is None:
        return None
    return session.exec(
        select(User).join(UserAccessGroupLink, UserAccessGroupLink.user_id == User.id)
        .where(UserAccessGroupLink.accessgroup_id == group.id, UserAccessGroupLink.role == Role.OWNER.db_value)
    ).first()


def transfer_ownership(session: Session, instrument, new_owner: User) -> Optional[User]:
    """Make `new_owner` the owner member; the previous owner stays as admin, as for projects."""
    if new_owner.is_service_account:
        raise HTTPException(status_code=422, detail="A service account cannot own an instrument")
    group = require_members_group(session, instrument.unique_id)
    previous_owner = None
    for link in session.exec(select(UserAccessGroupLink).where(
        UserAccessGroupLink.accessgroup_id == group.id, UserAccessGroupLink.role == Role.OWNER.db_value,
    )).all():
        previous_owner = session.get(User, link.user_id) or previous_owner
        link.role = Role.ADMIN.db_value
        session.add(link)
    session.flush()
    link = member_link(session, group, new_owner)
    if link is None:
        session.add(UserAccessGroupLink(user_id=new_owner.id, accessgroup_id=group.id, role=Role.OWNER.db_value))
    else:
        link.role = Role.OWNER.db_value
        session.add(link)
    instrument.owner = new_owner.unique_id
    instrument.owner_user_id = new_owner.id
    session.add(instrument)
    return previous_owner


GRANT_CHECKS = {
    "operator_grants_off_assignment": """
        SELECT count(*) FROM resourceacl acl JOIN accessgroup g ON g.id = acl.accessgroup_id
        WHERE g.group_type = 'instrument'
          AND NOT EXISTS (SELECT 1 FROM dataset d WHERE d.unique_id = acl.mfid AND d.instrument_mfid = g.group_name)""",
    "member_grants_off_assignment": """
        SELECT count(*) FROM resourceacl acl JOIN accessgroup g ON g.id = acl.accessgroup_id
        WHERE g.group_type = 'instrument_member' AND g.group_name <> acl.mfid || :suffix
          AND NOT EXISTS (SELECT 1 FROM dataset d WHERE d.unique_id = acl.mfid AND d.instrument_mfid || :suffix = g.group_name)""",
    "datasets_missing_operator_grant": """
        SELECT count(*) FROM dataset d WHERE d.instrument_mfid IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM resourceacl acl JOIN accessgroup g ON g.id = acl.accessgroup_id
            WHERE acl.mfid = d.unique_id AND g.group_type = 'instrument' AND g.group_name = d.instrument_mfid)""",
    "datasets_missing_member_grant": """
        SELECT count(*) FROM dataset d WHERE d.instrument_mfid IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM resourceacl acl JOIN accessgroup g ON g.id = acl.accessgroup_id
            WHERE acl.mfid = d.unique_id AND g.group_type = 'instrument_member' AND g.group_name = d.instrument_mfid || :suffix)""",
    "person_or_project_grants_on_instruments": """
        SELECT count(*) FROM resourceacl acl JOIN instrument i ON i.unique_id = acl.mfid
        JOIN accessgroup g ON g.id = acl.accessgroup_id WHERE g.group_type IN ('identity', 'project')""",
    "instruments_without_members_grant": """
        SELECT count(*) FROM instrument i WHERE NOT EXISTS (
            SELECT 1 FROM resourceacl acl JOIN accessgroup g ON g.id = acl.accessgroup_id
            WHERE acl.mfid = i.unique_id AND g.group_type = 'instrument_member' AND acl.permission = 'owner')""",
}


def grant_problems(session: Session) -> dict[str, int]:
    """Counts of instrument grants that do not match membership and dataset assignment; all zero when consistent."""
    from sqlalchemy import text

    return {name: session.execute(text(sql), {"suffix": MEMBERS_SUFFIX} if ":suffix" in sql else {}).scalar()
            for name, sql in GRANT_CHECKS.items()}


REPAIR_STATEMENTS = (
    """INSERT INTO accessgroup (group_name, group_type)
       SELECT i.unique_id || :suffix, 'instrument_member' FROM instrument i
       WHERE NOT EXISTS (SELECT 1 FROM accessgroup g WHERE g.group_name = i.unique_id || :suffix)""",
    """INSERT INTO useraccessgrouplink (user_id, accessgroup_id, role)
       SELECT person.user_id, members.id,
              CASE WHEN acl.permission = 'owner' AND EXISTS (
                       SELECT 1 FROM useraccessgrouplink current_owner
                       WHERE current_owner.accessgroup_id = members.id AND current_owner.role = 'owner')
                   THEN 'admin' ELSE acl.permission END
       FROM resourceacl acl
       JOIN instrument i ON i.unique_id = acl.mfid
       JOIN accessgroup identity_group ON identity_group.id = acl.accessgroup_id AND identity_group.group_type = 'identity'
       JOIN useraccessgrouplink person ON person.accessgroup_id = identity_group.id AND person.role = 'owner'
       JOIN "user" u ON u.id = person.user_id AND NOT COALESCE(u.is_service_account, false)
       JOIN accessgroup members ON members.group_name = i.unique_id || :suffix AND members.group_type = 'instrument_member'
       WHERE NOT EXISTS (SELECT 1 FROM useraccessgrouplink existing
                         WHERE existing.user_id = person.user_id AND existing.accessgroup_id = members.id)""",
    """DELETE FROM resourceacl WHERE mfid IN (SELECT unique_id FROM instrument) AND accessgroup_id IN (
           SELECT id FROM accessgroup WHERE group_type IN ('identity', 'project'))""",
    """INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
       SELECT i.unique_id, members.id, true, true, 'owner' FROM instrument i
       JOIN accessgroup members ON members.group_name = i.unique_id || :suffix AND members.group_type = 'instrument_member'
       WHERE NOT EXISTS (SELECT 1 FROM resourceacl acl WHERE acl.mfid = i.unique_id AND acl.accessgroup_id = members.id)""",
    """DELETE FROM resourceacl WHERE accessgroup_id IN (SELECT id FROM accessgroup WHERE group_type = 'instrument_member')
       AND NOT EXISTS (SELECT 1 FROM accessgroup g WHERE g.id = resourceacl.accessgroup_id AND g.group_name = resourceacl.mfid || :suffix)
       AND NOT EXISTS (SELECT 1 FROM dataset d JOIN accessgroup g ON g.group_name = d.instrument_mfid || :suffix
                       WHERE d.unique_id = resourceacl.mfid AND g.id = resourceacl.accessgroup_id)""",
    """INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
       SELECT d.unique_id, members.id, true, false, 'viewer' FROM dataset d
       JOIN accessgroup members ON members.group_name = d.instrument_mfid || :suffix AND members.group_type = 'instrument_member'
       WHERE NOT EXISTS (SELECT 1 FROM resourceacl acl WHERE acl.mfid = d.unique_id AND acl.accessgroup_id = members.id)""",
    """DELETE FROM resourceacl WHERE accessgroup_id IN (SELECT id FROM accessgroup WHERE group_type = 'instrument')
       AND NOT EXISTS (SELECT 1 FROM dataset d JOIN accessgroup g ON g.group_name = d.instrument_mfid
                       WHERE d.unique_id = resourceacl.mfid AND g.id = resourceacl.accessgroup_id)""",
    """INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
       SELECT d.unique_id, operator_group.id, true, true, 'contributor' FROM dataset d
       JOIN accessgroup operator_group ON operator_group.group_name = d.instrument_mfid AND operator_group.group_type = 'instrument'
       WHERE NOT EXISTS (SELECT 1 FROM resourceacl acl WHERE acl.mfid = d.unique_id AND acl.accessgroup_id = operator_group.id)""",
)


def repair_grants(session: Session) -> None:
    """Bring instrument grants in line with membership and dataset assignment, as the 4.0.0 migration does.
    Needed only for rows an earlier release wrote after that migration ran. The caller commits."""
    from sqlalchemy import text

    for sql in REPAIR_STATEMENTS:
        session.execute(text(sql), {"suffix": MEMBERS_SUFFIX})
