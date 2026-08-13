"""Resource access grants: /resources/{mfid}/access.

Replaces the /access_groups pair, which could grant but never revoke, returned
group names without roles, and made callers know the internal group naming.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import (
    AccessGroup, Instrument, Project, Resource, ResourceACL, User, UserAccessGroupLink)
from app.utils.permissions import (
    PUBLIC_GROUP_NAME, GroupMembership, PlatformRole, Principal, Role)

MFID = "ds-1"
OWNER_ORCID = "0000-0001-2345-6789"
FRIEND_ORCID = "0000-0002-3456-7890"
PROJ = "proj_x"

TABLES = (AccessGroup, Instrument, Project, Resource, ResourceACL, User,
          UserAccessGroupLink)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in TABLES])
    with Session(engine) as s:
        s.add(Resource(mfid=MFID, resource_type="dataset"))
        for orcid in (OWNER_ORCID, FRIEND_ORCID):
            s.add(User(first_name="A", last_name="B", email=f"{orcid}@x", unique_id=orcid))
            s.add(AccessGroup(group_name=orcid))
        proj = Project(project_id=PROJ, organization="LBNL", project_lead_orcid=OWNER_ORCID)
        s.add(proj)
        s.commit()
        s.refresh(proj)
        s.add(AccessGroup(group_name=proj.unique_id))
        s.add(AccessGroup(group_name=PUBLIC_GROUP_NAME))
        s.commit()
        owner_gid = s.exec(select(AccessGroup).where(
            AccessGroup.group_name == OWNER_ORCID)).one().id
        s.add(ResourceACL(mfid=MFID, accessgroup_id=owner_gid,
                          read=True, write=True, role=Role.OWNER.db_value))
        s.commit()
        yield s


def gid(session, name):
    return session.exec(select(AccessGroup).where(AccessGroup.group_name == name)).one().id


def owner(session):
    return Principal(user_id=OWNER_ORCID,
                     groups=(GroupMembership(gid(session, OWNER_ORCID), OWNER_ORCID, Role.OWNER),))


class TestGroupKind:
    def test_classifies_each_principal_type(self, session):
        from app.api.routes.resources import _group_kind
        proj = session.exec(select(Project).where(Project.project_id == PROJ)).one()
        assert _group_kind(session, OWNER_ORCID) == "user"
        assert _group_kind(session, proj.unique_id) == "project"
        assert _group_kind(session, PUBLIC_GROUP_NAME) == "public"
        assert _group_kind(session, "admin") == "system"
        assert _group_kind(session, "nothing-like-this") == "unknown"

    def test_distinguishes_service_accounts(self, session):
        from app.api.routes.resources import _group_kind
        session.add(User(first_name="", last_name="", email=None,
                         unique_id="0tkn2knjast3h0008nyq9zps2c", is_service_account=True))
        session.add(AccessGroup(group_name="0tkn2knjast3h0008nyq9zps2c"))
        session.commit()
        assert _group_kind(session, "0tkn2knjast3h0008nyq9zps2c") == "service_account"


class TestResolveGrantee:
    def test_user_by_orcid(self, session):
        from app.api.routes.resources import _resolve_grantee
        ag, canonical = _resolve_grantee(session, "users", FRIEND_ORCID)
        assert canonical == FRIEND_ORCID and ag.group_name == FRIEND_ORCID

    def test_project_by_id(self, session):
        from app.api.routes.resources import _resolve_grantee
        proj = session.exec(select(Project).where(Project.project_id == PROJ)).one()
        ag, canonical = _resolve_grantee(session, "projects", PROJ)
        assert canonical == proj.unique_id and ag.group_name == proj.unique_id

    def test_unknown_project_is_404(self, session):
        from app.api.routes.resources import _resolve_grantee
        with pytest.raises(HTTPException) as exc:
            _resolve_grantee(session, "projects", "no-such-project")
        assert exc.value.status_code == 404

    def test_a_username_cannot_be_read_as_a_project(self, session):
        """Typed path segments are what stop the namespaces colliding."""
        from app.api.routes.resources import _resolve_grantee
        with pytest.raises(HTTPException):
            _resolve_grantee(session, "projects", FRIEND_ORCID)

    def test_service_account_resolves_by_mfid(self, session):
        """An SA's identity is an mfid, not an ORCID — is_orcid rejects it, so
        it would otherwise fall through to a username lookup and 404."""
        from app.api.routes.resources import _resolve_grantee
        sa_id = "0tkn2knjast3h0008nyq9zps2c"
        session.add(User(first_name="", last_name="", email=None,
                         unique_id=sa_id, is_service_account=True))
        session.add(AccessGroup(group_name=sa_id))
        session.commit()
        ag, canonical = _resolve_grantee(session, "users", sa_id)
        assert canonical == sa_id


def proj_gid(session):
    proj = session.exec(select(Project).where(Project.project_id == PROJ)).one()
    return gid(session, proj.unique_id)


class TestManageAclCap:
    def test_owner_may_grant_anything_up_to_owner(self, session):
        from app.utils.auth_utils import require_grant
        require_grant(session, owner(session), MFID, Role.EDITOR)
        require_grant(session, owner(session), MFID, Role.OWNER)

    def test_admin_cannot_mint_an_owner(self, session):
        from app.utils.auth_utils import require_grant
        session.add(ResourceACL(mfid=MFID, accessgroup_id=proj_gid(session),
                                read=True, write=True, role=Role.ADMIN.db_value))
        session.commit()
        p = Principal(user_id="x",
                      groups=(GroupMembership(proj_gid(session), PROJ, Role.ADMIN),))
        require_grant(session, p, MFID, Role.EDITOR)      # fine
        with pytest.raises(HTTPException):
            require_grant(session, p, MFID, Role.OWNER)

    def test_admin_cannot_overwrite_an_owner_grant(self, session):
        from app.utils.auth_utils import require_grant
        session.add(ResourceACL(mfid=MFID, accessgroup_id=proj_gid(session),
                                read=True, write=True, role=Role.ADMIN.db_value))
        session.commit()
        p = Principal(user_id="x",
                      groups=(GroupMembership(proj_gid(session), PROJ, Role.ADMIN),))
        with pytest.raises(HTTPException):
            require_grant(session, p, MFID, Role.VIEWER, current=Role.OWNER)

    def test_a_mere_editor_cannot_manage_acls_at_all(self, session):
        from app.utils.auth_utils import require_grant
        session.add(ResourceACL(mfid=MFID, accessgroup_id=proj_gid(session),
                                read=True, write=True, role=Role.EDITOR.db_value))
        session.commit()
        p = Principal(user_id="x",
                      groups=(GroupMembership(proj_gid(session), PROJ, Role.EDITOR),))
        with pytest.raises(HTTPException):
            require_grant(session, p, MFID, Role.VIEWER)

    def test_platform_admin_bypasses(self, session):
        from app.utils.auth_utils import require_grant
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_grant(session, admin, MFID, Role.OWNER, current=Role.OWNER)
