"""Project routes check permission against the project resource.

Projects became resources in phase 1, so proj_id is the project's mfid and
these are ordinary require_permission calls. The fallback exists because
projects only gained ACL entries in that backfill — without it, deploying this
before the backfill locks every project route.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import (
    AccessGroup, Project, Resource, ResourceACL, User, UserAccessGroupLink)
from app.utils.auth_utils import require_permission
from app.utils.permissions import GroupMembership, Permission, PlatformRole, Principal, Role

PROJ = "proj_x"
GID = 1


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(
        engine, tables=[m.__table__ for m in (AccessGroup, Project, Resource, ResourceACL, User,
                              UserAccessGroupLink)])
    with Session(engine) as s:
        s.add(AccessGroup(group_name=PROJ))
        s.add(Project(project_id=PROJ, organization="LBNL", project_lead_orcid="0-1"))
        s.commit()
        yield s


def migrated(session):
    """Give the project its resource row and self-granting ACL entry."""
    session.add(Resource(mfid=PROJ, resource_type="project"))
    session.add(ResourceACL(mfid=PROJ, accessgroup_id=GID, read=True, write=True,
                            permission=Role.OWNER.db_value))
    session.commit()


def member(role):
    return Principal(user_id="0000-0001", groups=(GroupMembership(GID, PROJ, role),))


class TestAfterMigration:
    """Effective role on a project equals standing in its group, because the
    project grants its own group `owner`."""

    @pytest.mark.parametrize("standing,permission,allowed", [
        (Role.VIEWER, Permission.READ, True),
        (Role.VIEWER, Permission.EDIT, False),
        (Role.CONTRIBUTOR, Permission.READ, True),
        (Role.CONTRIBUTOR, Permission.EDIT, False),
        (Role.EDITOR, Permission.READ, True),
        (Role.EDITOR, Permission.EDIT, True),
        (Role.ADMIN, Permission.EDIT, True),
        (Role.OWNER, Permission.TRANSFER, True),
        (Role.ADMIN, Permission.TRANSFER, False),
    ])
    def test_standing_decides(self, session, standing, permission, allowed):
        migrated(session)
        if allowed:
            require_permission(session, member(standing), PROJ, permission, legacy_group=PROJ)
        else:
            with pytest.raises(HTTPException):
                require_permission(session, member(standing), PROJ, permission, legacy_group=PROJ)

    def test_a_viewer_can_read_but_not_add_members(self, session):
        """The tightening: today any project member can add anyone."""
        migrated(session)
        require_permission(session, member(Role.VIEWER), PROJ, Permission.READ, legacy_group=PROJ)
        with pytest.raises(HTTPException):
            require_permission(session, member(Role.VIEWER), PROJ, Permission.EDIT, legacy_group=PROJ)

    def test_non_member_is_denied(self, session):
        migrated(session)
        outsider = Principal(user_id="0000-0009",
                             groups=(GroupMembership(99, "other", Role.OWNER),))
        with pytest.raises(HTTPException):
            require_permission(session, outsider, PROJ, Permission.READ, legacy_group=PROJ)

    def test_platform_admin_always_passes(self, session):
        migrated(session)
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_permission(session, admin, PROJ, Permission.TRANSFER, legacy_group=PROJ)


class TestBeforeMigration:
    """No ACL rows yet — fall back to the pre-role check rather than locking
    every project route."""

    def test_group_member_still_allowed(self, session):
        require_permission(session, member(Role.VIEWER), PROJ, Permission.EDIT, legacy_group=PROJ)

    def test_admin_group_still_allowed(self, session):
        p = Principal(user_id="0000-0001",
                      groups=(GroupMembership(2, "admin", Role.VIEWER),))
        require_permission(session, p, PROJ, Permission.EDIT, legacy_group=PROJ)

    def test_outsider_still_denied(self, session):
        outsider = Principal(user_id="0000-0009",
                             groups=(GroupMembership(99, "other", Role.OWNER),))
        with pytest.raises(HTTPException):
            require_permission(session, outsider, PROJ, Permission.READ, legacy_group=PROJ)


def test_new_members_get_contributor_not_null(session):
    """A null standing fails closed to viewer, so appending via the
    relationship would have left new members read-only."""
    from app.utils.user_utils import sql_add_user_to_access_group

    session.add(User(first_name="A", last_name="B", email="a@x", unique_id="0000-0002"))
    session.commit()
    sql_add_user_to_access_group("0000-0002", PROJ, session)
    session.commit()

    link = session.exec(select(UserAccessGroupLink)).first()
    assert link.role == Role.CONTRIBUTOR.db_value


class TestGrantCap:
    """You may only seat someone at or below your own role, and may not touch
    a member who already holds one above yours."""

    def _actor(self, session, role):
        migrated(session)
        return member(role)

    @pytest.mark.parametrize("actor,target,allowed", [
        (Role.EDITOR, Role.VIEWER, True),
        (Role.EDITOR, Role.CONTRIBUTOR, True),
        (Role.EDITOR, Role.EDITOR, True),
        (Role.EDITOR, Role.ADMIN, False),
        (Role.EDITOR, Role.OWNER, False),
        (Role.ADMIN, Role.ADMIN, True),
        (Role.ADMIN, Role.OWNER, False),
        (Role.OWNER, Role.OWNER, True),
    ])
    def test_cannot_grant_above_own_role(self, session, actor, target, allowed):
        from app.utils.auth_utils import require_grant
        p = self._actor(session, actor)
        if allowed:
            require_grant(session, p, PROJ, target, floor=Role.EDITOR)
        else:
            with pytest.raises(HTTPException) as exc:
                require_grant(session, p, PROJ, target, floor=Role.EDITOR)
            assert exc.value.status_code == 403

    def test_admin_cannot_demote_the_owner(self, session):
        """The reason the cap binds on the current role too."""
        from app.utils.auth_utils import require_grant
        p = self._actor(session, Role.ADMIN)
        with pytest.raises(HTTPException):
            require_grant(session, p, PROJ, Role.VIEWER, current=Role.OWNER, floor=Role.EDITOR)

    def test_owner_may_demote_an_admin(self, session):
        from app.utils.auth_utils import require_grant
        p = self._actor(session, Role.OWNER)
        require_grant(session, p, PROJ, Role.VIEWER, current=Role.ADMIN, floor=Role.EDITOR)

    def test_platform_admin_bypasses_the_cap(self, session):
        from app.utils.auth_utils import require_grant
        migrated(session)
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_grant(session, admin, PROJ, Role.OWNER, current=Role.OWNER, floor=Role.EDITOR)


def test_project_lead_owns_their_own_project(session):
    """create_project used ag.users.append, leaving role null — the lead would
    have landed as viewer, unable to edit the project they just made."""
    from app.utils.user_utils import sql_add_user_to_access_group

    session.add(User(first_name="L", last_name="P", email="l@x", unique_id="0000-0003"))
    session.commit()
    sql_add_user_to_access_group("0000-0003", PROJ, session, role=Role.OWNER)
    session.commit()

    link = session.exec(select(UserAccessGroupLink)).first()
    assert link.role == Role.OWNER.db_value
