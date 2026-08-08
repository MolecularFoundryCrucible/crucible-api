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
from app.utils.auth_utils import require_project_permission
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
                            role=Role.OWNER.db_value))
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
            require_project_permission(session, member(standing), PROJ, permission)
        else:
            with pytest.raises(HTTPException):
                require_project_permission(session, member(standing), PROJ, permission)

    def test_a_viewer_can_read_but_not_add_members(self, session):
        """The tightening: today any project member can add anyone."""
        migrated(session)
        require_project_permission(session, member(Role.VIEWER), PROJ, Permission.READ)
        with pytest.raises(HTTPException):
            require_project_permission(session, member(Role.VIEWER), PROJ, Permission.EDIT)

    def test_non_member_is_denied(self, session):
        migrated(session)
        outsider = Principal(user_id="0000-0009",
                             groups=(GroupMembership(99, "other", Role.OWNER),))
        with pytest.raises(HTTPException):
            require_project_permission(session, outsider, PROJ, Permission.READ)

    def test_platform_admin_always_passes(self, session):
        migrated(session)
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_project_permission(session, admin, PROJ, Permission.TRANSFER)


class TestBeforeMigration:
    """No ACL rows yet — fall back to the pre-role check rather than locking
    every project route."""

    def test_group_member_still_allowed(self, session):
        require_project_permission(session, member(Role.VIEWER), PROJ, Permission.EDIT)

    def test_admin_group_still_allowed(self, session):
        p = Principal(user_id="0000-0001",
                      groups=(GroupMembership(2, "admin", Role.VIEWER),))
        require_project_permission(session, p, PROJ, Permission.EDIT)

    def test_outsider_still_denied(self, session):
        outsider = Principal(user_id="0000-0009",
                             groups=(GroupMembership(99, "other", Role.OWNER),))
        with pytest.raises(HTTPException):
            require_project_permission(session, outsider, PROJ, Permission.READ)


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
