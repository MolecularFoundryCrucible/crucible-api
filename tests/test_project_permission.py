"""Project routes check permission against the project resource ACL."""
import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import (
    AccessGroup, Project, Resource, ResourceACL, User, UserAccessGroupLink)
from app.utils.auth_utils import fetch_acl_rows, require_permission, resource_capabilities
from app.utils.permissions import GroupMembership, Permission, PlatformRole, Principal, Role
from app.models.core_models import UserIdentity

PROJ = "proj_x"
GID = 1


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(
        engine, tables=[m.__table__ for m in (AccessGroup, Project, Resource, ResourceACL, User,
                              UserAccessGroupLink, UserIdentity)])
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
        principal = member(standing)
        acl_rows = fetch_acl_rows(session, PROJ)
        if allowed:
            require_permission(
                None, principal, PROJ, permission, acl_rows=acl_rows)
        else:
            with pytest.raises(HTTPException):
                require_permission(
                    None, principal, PROJ, permission, acl_rows=acl_rows)

        capabilities = resource_capabilities(
            None,
            principal,
            PROJ,
            manage_access_floor=Role.EDITOR,
            allow_peer_grants=False,
            status_permission=Permission.EDIT,
            acl_rows=acl_rows,
        )
        expected_max = None
        if standing >= Role.OWNER:
            expected_max = "admin"
        elif standing >= Role.ADMIN:
            expected_max = "editor"
        elif standing >= Role.EDITOR:
            expected_max = "contributor"
        assert capabilities.can_edit is (standing >= Role.EDITOR)
        assert capabilities.can_manage_access is (standing >= Role.EDITOR)
        assert capabilities.can_change_status is (standing >= Role.EDITOR)
        assert capabilities.can_transfer is (standing >= Role.OWNER)
        assert capabilities.max_grant_role == expected_max

    def test_a_viewer_can_read_but_not_add_members(self, session):
        """The tightening: today any project member can add anyone."""
        migrated(session)
        require_permission(session, member(Role.VIEWER), PROJ, Permission.READ)
        with pytest.raises(HTTPException):
            require_permission(session, member(Role.VIEWER), PROJ, Permission.EDIT)

    def test_non_member_is_denied(self, session):
        migrated(session)
        outsider = Principal(user_id="0000-0009",
                             groups=(GroupMembership(99, "other", Role.OWNER),))
        with pytest.raises(HTTPException):
            require_permission(session, outsider, PROJ, Permission.READ)

    def test_platform_admin_always_passes(self, session):
        migrated(session)
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_permission(session, admin, PROJ, Permission.TRANSFER)


class TestMissingAcl:
    def test_group_member_is_denied(self, session):
        with pytest.raises(HTTPException):
            require_permission(session, member(Role.OWNER), PROJ, Permission.READ)

    def test_admin_group_member_is_denied(self, session):
        p = Principal(user_id="0000-0001",
                      groups=(GroupMembership(2, "admin", Role.VIEWER),))
        with pytest.raises(HTTPException):
            require_permission(session, p, PROJ, Permission.READ)

    def test_platform_admin_is_allowed(self, session):
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_permission(session, admin, PROJ, Permission.READ)


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
    """Project membership uses a strict role hierarchy."""

    def _actor(self, session, role):
        migrated(session)
        return member(role)

    @pytest.mark.parametrize("actor,target,allowed", [
        (Role.EDITOR, Role.VIEWER, True),
        (Role.EDITOR, Role.CONTRIBUTOR, True),
        (Role.EDITOR, Role.EDITOR, False),
        (Role.EDITOR, Role.ADMIN, False),
        (Role.EDITOR, Role.OWNER, False),
        (Role.ADMIN, Role.EDITOR, True),
        (Role.ADMIN, Role.ADMIN, False),
        (Role.ADMIN, Role.OWNER, False),
        (Role.OWNER, Role.ADMIN, True),
        (Role.OWNER, Role.OWNER, False),
    ])
    def test_may_only_grant_below_own_role(self, session, actor, target, allowed):
        from app.utils.auth_utils import require_grant
        p = self._actor(session, actor)
        if allowed:
            require_grant(
                session,
                p,
                PROJ,
                target,
                floor=Role.EDITOR,
                allow_peer=False,
            )
        else:
            with pytest.raises(HTTPException) as exc:
                require_grant(
                    session,
                    p,
                    PROJ,
                    target,
                    floor=Role.EDITOR,
                    allow_peer=False,
                )
            assert exc.value.status_code == 403

    def test_admin_cannot_demote_the_owner(self, session):
        """The reason the cap binds on the current role too."""
        from app.utils.auth_utils import require_grant
        p = self._actor(session, Role.ADMIN)
        with pytest.raises(HTTPException):
            require_grant(
                session,
                p,
                PROJ,
                Role.VIEWER,
                current=Role.OWNER,
                floor=Role.EDITOR,
                allow_peer=False,
            )

    def test_editor_cannot_demote_an_editor(self, session):
        from app.utils.auth_utils import require_grant
        p = self._actor(session, Role.EDITOR)
        with pytest.raises(HTTPException):
            require_grant(
                session,
                p,
                PROJ,
                Role.CONTRIBUTOR,
                current=Role.EDITOR,
                floor=Role.EDITOR,
                allow_peer=False,
            )

    def test_owner_may_demote_an_admin(self, session):
        from app.utils.auth_utils import require_grant
        p = self._actor(session, Role.OWNER)
        require_grant(
            session,
            p,
            PROJ,
            Role.VIEWER,
            current=Role.ADMIN,
            floor=Role.EDITOR,
            allow_peer=False,
        )

    def test_platform_admin_bypasses_the_cap(self, session):
        from app.utils.auth_utils import require_grant
        migrated(session)
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_grant(
            session,
            admin,
            PROJ,
            Role.OWNER,
            current=Role.OWNER,
            floor=Role.EDITOR,
            allow_peer=False,
        )


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
