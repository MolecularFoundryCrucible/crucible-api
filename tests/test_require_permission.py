"""require_permission and permission_filter against a real Postgres.

Both depend on the `role` column comparing by ENUM ORDER, not alphabetically.
The two disagree completely:

    enum:         viewer < contributor < editor < admin < owner
    alphabetical: admin  < contributor < editor < owner < viewer

The model declares the column as str for SQLite's sake, so nothing in Python
enforces this — only Postgres does. SQLite cannot check it, which is why these
tests need a real server.
"""
import pytest
from sqlalchemy import select as sa_select, text
from sqlmodel import Session
from fastapi import HTTPException

from app.models.core_models import Dataset
from app.utils.auth_utils import fetch_acl_rows, permission_filter, require_permission
from app.utils.permissions import (
    GroupMembership, Permission, PlatformRole, Principal, Role,
)
from tests.pg_fixture import LEAD, MEMBER, PG_URL, SA, STAFF, build_migrated_engine, principal_for

pytestmark = pytest.mark.skipif(not PG_URL, reason="CRUCIBLE_TEST_PG_URL not set")


@pytest.fixture(scope="module")
def migrated():
    return build_migrated_engine()


def visible(engine, principal, permission=Permission.READ):
    """Dataset unique_ids the filter admits."""
    with Session(engine) as session:
        stmt = sa_select(Dataset.unique_id).where(
            permission_filter(principal, Dataset, permission))
        return sorted(session.execute(stmt).scalars().all())


class TestEnumOrderingReachesSQL:
    def test_role_comparison_uses_enum_order(self, migrated):
        """Postgres orders by declaration, and Role must match it exactly."""
        with Session(migrated) as session:
            got = [r for (r,) in session.execute(text(
                "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'resource_role' ORDER BY e.enumsortorder")).all()]
        assert got == ["viewer", "contributor", "editor", "admin", "owner"]
        assert got == [r.db_value for r in Role], "Role and the DB enum disagree"

    @pytest.mark.parametrize("threshold,expected", [
        (Role.VIEWER, {"viewer", "contributor", "editor", "admin", "owner"}),
        (Role.EDITOR, {"editor", "admin", "owner"}),
        (Role.OWNER, {"owner"}),
    ])
    def test_threshold_selects_the_right_roles(self, migrated, threshold, expected):
        """Alphabetical ordering would put viewer above owner and admin below
        contributor — this is what catches that."""
        with Session(migrated) as session:
            got = set(r for (r,) in session.execute(text(
                "SELECT DISTINCT role::text FROM resourceacl WHERE role >= :t"),
                {"t": threshold.db_value}).all())
        assert got <= expected, f"selected roles below {threshold.name}: {got - expected}"


class TestPermissionFilterMatchesEffectiveRole:
    """The filter is a decomposition of MIN/MAX into SQL. If it ever disagrees
    with effective_role, list endpoints leak or hide rows relative to the
    single-resource check on the same data."""

    @pytest.mark.parametrize("orcid", [LEAD, MEMBER, STAFF, SA])
    @pytest.mark.parametrize("permission", [Permission.READ, Permission.EDIT, Permission.DELETE])
    def test_filter_agrees_with_per_resource_check(self, migrated, orcid, permission):
        from app.utils.permissions import has_permission

        principal = principal_for(migrated, orcid)
        by_filter = set(visible(migrated, principal, permission))

        with Session(migrated) as session:
            all_ids = session.execute(sa_select(Dataset.unique_id)).scalars().all()
            by_check = {
                mfid for mfid in all_ids
                if has_permission(principal, fetch_acl_rows(session, mfid), permission)
            }

        assert by_filter == by_check, (
            f"{orcid} / {permission.value}: filter and per-resource check disagree on "
            f"{by_filter ^ by_check}"
        )


class TestRequirePermission:
    def test_allows_when_permitted(self, migrated):
        with Session(migrated) as session:
            role = require_permission(session, principal_for(migrated, MEMBER),
                                      "ds-priv", Permission.DELETE)
        assert role is Role.OWNER

    def test_raises_403_when_not(self, migrated):
        with Session(migrated) as session:
            with pytest.raises(HTTPException) as exc:
                require_permission(session, principal_for(migrated, STAFF),
                                   "ds-priv", Permission.READ)
        assert exc.value.status_code == 403

    def test_member_can_edit_but_not_delete_a_peers_dataset(self, migrated):
        p = principal_for(migrated, MEMBER)
        with Session(migrated) as session:
            require_permission(session, p, "ds-pub", Permission.EDIT)   # no raise
            with pytest.raises(HTTPException):
                require_permission(session, p, "ds-pub", Permission.DELETE)

    def test_platform_admin_short_circuits(self, migrated):
        """Returns None: their access has no effective role behind it."""
        admin = Principal(user_id="x", platform_role=PlatformRole.ADMIN)
        with Session(migrated) as session:
            assert require_permission(session, admin, "ds-priv", Permission.DELETE) is None

    def test_unknown_resource_is_denied_not_crashed(self, migrated):
        with Session(migrated) as session:
            with pytest.raises(HTTPException) as exc:
                require_permission(session, principal_for(migrated, LEAD),
                                   "does-not-exist", Permission.READ)
        assert exc.value.status_code == 403


class TestFilterEdgeCases:
    def test_platform_admin_sees_everything(self, migrated):
        admin = Principal(user_id="x", platform_role=PlatformRole.ADMIN)
        with Session(migrated) as session:
            total = len(session.execute(sa_select(Dataset.unique_id)).scalars().all())
        assert len(visible(migrated, admin)) == total

    def test_principal_with_no_groups_sees_nothing(self, migrated):
        assert visible(migrated, Principal(user_id="nobody")) == []

    def test_standing_below_the_threshold_removes_the_group(self, migrated):
        """A viewer in a project sees its data but cannot edit it, even though
        the project's grant is admin — the cap is on the Python side."""
        with Session(migrated) as session:
            gid = session.execute(text(
                "SELECT id FROM accessgroup WHERE group_name = 'proj_x'")).scalar()
        viewer = Principal(user_id="v",
                           groups=(GroupMembership(gid, "proj_x", Role.VIEWER),))
        assert visible(migrated, viewer, Permission.READ) != []
        assert visible(migrated, viewer, Permission.EDIT) == []
