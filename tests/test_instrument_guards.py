"""Instrument routes: who may edit the record, and who may retire it.

PATCH was unguarded — any authenticated user could rename or re-point someone
else's instrument. Status moves to its own route because retiring a machine is
a lifecycle transition, not a metadata edit, and the instrument's own service
account holds editor and must not be able to decommission it.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import AccessGroup, Instrument, Resource, ResourceACL, User
from app.models.instrument import InstrumentUpdate
from app.utils.auth_utils import require_permission
from app.utils.permissions import GroupMembership, Permission, PlatformRole, Principal, Role

MFID, NAME = "inst-1", "xrd-1"
TABLES = (AccessGroup, Instrument, Resource, ResourceACL, User)


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in TABLES])
    with Session(engine) as s:
        s.add(Instrument(unique_id=MFID, instrument_name=NAME, owner="o", location="b67"))
        s.add(Resource(mfid=MFID, resource_type="instrument"))
        s.add(AccessGroup(group_name=NAME))
        s.commit()
        yield s


def gid(session):
    return session.exec(select(AccessGroup).where(AccessGroup.group_name == NAME)).one().id


def op(session, role):
    return Principal(user_id="sa-1", is_service_account=True,
                     groups=(GroupMembership(gid(session), NAME, role),))


def granted(session, role):
    session.add(ResourceACL(mfid=MFID, accessgroup_id=gid(session),
                            read=True, write=True, permission=role.db_value))
    session.commit()


class TestEditingTheRecord:
    def test_operator_with_editor_may_edit(self, session):
        granted(session, Role.EDITOR)
        require_permission(session, op(session, Role.ADMIN), MFID, Permission.EDIT,
                           legacy_group=NAME)

    def test_a_stranger_may_not(self, session):
        granted(session, Role.EDITOR)
        stranger = Principal(user_id="0000-0009",
                             groups=(GroupMembership(99, "other", Role.OWNER),))
        with pytest.raises(HTTPException) as exc:
            require_permission(session, stranger, MFID, Permission.EDIT, legacy_group=NAME)
        assert exc.value.status_code == 403

    def test_before_migration_group_membership_still_works(self, session):
        """No ACL rows yet — instruments only get them at registration."""
        require_permission(session, op(session, Role.VIEWER), MFID, Permission.EDIT,
                           legacy_group=NAME)


class TestRetiring:
    def test_the_service_account_cannot_decommission(self, session):
        """It holds editor on the record, deliberately below the delete bar."""
        granted(session, Role.EDITOR)
        with pytest.raises(HTTPException):
            require_permission(session, op(session, Role.ADMIN), MFID, Permission.DELETE,
                               legacy_group=NAME)

    def test_an_admin_can(self, session):
        granted(session, Role.ADMIN)
        require_permission(session, op(session, Role.ADMIN), MFID, Permission.DELETE,
                           legacy_group=NAME)

    def test_platform_admin_can(self, session):
        granted(session, Role.EDITOR)
        admin = Principal(user_id="a", platform_role=PlatformRole.ADMIN)
        require_permission(session, admin, MFID, Permission.DELETE, legacy_group=NAME)


def test_status_is_not_editable_through_patch():
    """It has a higher bar, so it must not ride along in a metadata payload."""
    assert "status" not in InstrumentUpdate.model_fields


def test_status_values_are_constrained():
    from app.api.routes.instruments import VALID_INSTRUMENT_STATUSES
    assert set(VALID_INSTRUMENT_STATUSES) == {"active", "maintenance", "decommissioned"}
