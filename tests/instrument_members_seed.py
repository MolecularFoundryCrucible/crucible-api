from sqlmodel import select

from app.services.instrument_members import members_group_name
from app.tables import AccessGroup, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, Role


def seed_instrument_members(session, instrument_mfid: str, members: dict) -> int:
    """The instrument's members group, holding the record's owner grant, with `members` ({unique_id: Role}) in it."""
    group = AccessGroup(group_name=members_group_name(instrument_mfid), group_type=AccessGroupType.INSTRUMENT_MEMBER.value)
    session.add(group)
    session.flush()
    session.add(ResourceACL(mfid=instrument_mfid, accessgroup_id=group.id, read=True, write=True, permission=Role.OWNER.db_value))
    for unique_id, role in members.items():
        user_id = session.exec(select(User.id).where(User.unique_id == unique_id)).one()
        session.add(UserAccessGroupLink(user_id=user_id, accessgroup_id=group.id, role=role.db_value))
    session.flush()
    return group.id
