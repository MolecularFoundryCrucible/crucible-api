"""End-to-end test of the phase 1 permission migration against a real Postgres.

The migration is Postgres-specific — enum types, casts, CTEs in UPDATE,
DELETE ... USING — so SQLite cannot validate it and the pure matrix in
test_permissions.py cannot either. This runs the actual SQL file and the actual
backfill against a throwaway database, then asserts on effective roles computed
from the resulting rows.

Skipped unless CRUCIBLE_TEST_PG_URL is set. See tests/pg_fixture.py.
"""
import pytest
from sqlalchemy import text
from sqlmodel import Session

from app.utils.permissions import (
    GroupMembership, Permission, PlatformRole, Principal, Role,
    effective_role, has_permission,
)
from tests.pg_fixture import (
    LEAD, MEMBER, PLATFORM_ADMIN, PG_URL, SA, STAFF,
    acl_for, build_migrated_engine, principal_for,
)

pytestmark = pytest.mark.skipif(not PG_URL, reason="CRUCIBLE_TEST_PG_URL not set")


@pytest.fixture(scope="module")
def migrated():
    """Fresh schema, real migration SQL, real backfill. Module-scoped: the
    migration is the thing under test, so it runs once and every assertion
    reads the result."""
    return build_migrated_engine()


@pytest.mark.parametrize("orcid,mfid,expected", [
    # The lead owns what they created and reaches the rest through the project.
    (LEAD, "ds-pub", Role.OWNER),
    (LEAD, "ds-priv", Role.ADMIN),
    # Standing owner in the project group, and the project grants its own group
    # owner — so the lead can transfer the project. Regression guard: the
    # generic project grant of `admin` must not overwrite this self-entry.
    (LEAD, "proj_x", Role.OWNER),
    # Existing members backfill to editor, preserving today's behaviour where
    # any project member can edit any project resource.
    (MEMBER, "ds-pub", Role.EDITOR),
    (MEMBER, "ds-priv", Role.OWNER),
    (MEMBER, "proj_x", Role.EDITOR),
    # Facility staff lose instrument-derived access. What remains on ds-pub is
    # viewer through the virtual public group, which every principal holds —
    # not anything the instrument group granted. ds-priv shows the real effect.
    (STAFF, "ds-pub", Role.VIEWER),
    (STAFF, "ds-priv", None),
    # The service account reaches its own instrument's output and nothing else.
    (SA, "ds-pub", Role.CONTRIBUTOR),
    (SA, "ds-priv", None),
    (SA, "proj_x", None),
])
def test_effective_role_after_migration(migrated, orcid, mfid, expected):
    assert effective_role(principal_for(migrated, orcid), acl_for(migrated, mfid)) == expected


def test_lead_can_transfer_their_project(migrated):
    p, acl = principal_for(migrated, LEAD), acl_for(migrated, "proj_x")
    assert has_permission(p, acl, Permission.TRANSFER)


def test_member_cannot_transfer_the_project(migrated):
    p, acl = principal_for(migrated, MEMBER), acl_for(migrated, "proj_x")
    assert not has_permission(p, acl, Permission.TRANSFER)


def test_member_can_edit_but_not_delete_a_peers_dataset(migrated):
    p, acl = principal_for(migrated, MEMBER), acl_for(migrated, "ds-pub")
    assert has_permission(p, acl, Permission.EDIT)
    assert not has_permission(p, acl, Permission.DELETE)


def test_service_account_creates_but_cannot_edit_researcher_data(migrated):
    p, acl = principal_for(migrated, SA), acl_for(migrated, "ds-pub")
    assert has_permission(p, acl, Permission.CREATE)
    assert not has_permission(p, acl, Permission.EDIT)


def test_platform_admin_bypasses_without_acl_rows(migrated):
    p = principal_for(migrated, PLATFORM_ADMIN)
    assert p.platform_role is PlatformRole.ADMIN
    assert effective_role(p, acl_for(migrated, "ds-priv")) is None
    assert has_permission(p, acl_for(migrated, "ds-priv"), Permission.DELETE)


class TestServiceAccountsAreNotPromoted:
    """Service accounts sit in the admin group as a historical workaround. The
    backfill must not turn that into platform_role, which would make the
    platform-wide access permanent and unconditional — the exact thing scoping
    instruments is meant to remove."""

    def test_service_account_in_admin_group_keeps_platform_role_none(self, migrated):
        assert principal_for(migrated, SA).platform_role is PlatformRole.NONE

    def test_human_in_admin_group_is_promoted(self, migrated):
        assert principal_for(migrated, PLATFORM_ADMIN).platform_role is PlatformRole.ADMIN

    def test_service_account_gets_no_bypass_from_admin_membership(self, migrated):
        """It is still in the group; the group must simply confer nothing."""
        p = principal_for(migrated, SA)
        assert p.role_in("admin") is not None, "fixture should keep it in the group"
        assert not has_permission(p, acl_for(migrated, "ds-priv"), Permission.READ)

    def test_admin_group_confers_nothing_through_the_acl(self, migrated):
        with Session(migrated) as session:
            rows = session.execute(text("""
                SELECT count(*) FROM resourceacl a
                JOIN accessgroup ag ON ag.id = a.accessgroup_id
                WHERE ag.group_name = 'admin'
            """)).scalar()
        assert rows == 0


class TestAdminGroupBypassSurvivesRowDeletion:
    """The migration deletes ~360k admin ACL rows. On production every
    admin-group member is a service account whose only other group is its own
    ORCID group, so those rows are their entire access. platform_role is not
    consulted until cutover, so without a bypass in the pre-role helpers this
    deletion would cut off every instrument the moment phase 1 ran."""

    def _ag_ids(self, engine, orcid):
        with Session(engine) as session:
            return session.execute(text("""
                SELECT ag.id FROM useraccessgrouplink l
                JOIN accessgroup ag ON ag.id = l.accessgroup_id
                JOIN "user" u ON u.id = l.user_id WHERE u.orcid = :o
            """), {"o": orcid}).scalars().all()

    def test_admin_group_member_still_reads_after_rows_are_deleted(self, migrated):
        from app.utils.auth_utils import require_read_access
        ag_ids = self._ag_ids(migrated, SA)
        with Session(migrated) as session:
            require_read_access(session, "ds-priv", ag_ids)  # must not raise

    def test_admin_group_member_still_writes_after_rows_are_deleted(self, migrated):
        from app.utils.auth_utils import require_write_access
        ag_ids = self._ag_ids(migrated, SA)
        with Session(migrated) as session:
            require_write_access(session, ag_ids, ["ds-priv"])  # must not raise

    def test_non_admin_is_still_denied(self, migrated):
        """The bypass must key on admin membership, not simply always pass."""
        from fastapi import HTTPException
        from app.utils.auth_utils import require_read_access
        ag_ids = self._ag_ids(migrated, STAFF)
        with Session(migrated) as session:
            with pytest.raises(HTTPException) as exc:
                require_read_access(session, "ds-priv", ag_ids)
            assert exc.value.status_code == 403

    def test_the_admin_acl_rows_really_are_gone(self, migrated):
        """Guards against this passing for the boring reason."""
        with Session(migrated) as session:
            n = session.execute(text("""
                SELECT count(*) FROM resourceacl a
                JOIN accessgroup ag ON ag.id = a.accessgroup_id
                WHERE ag.group_name = 'admin'
            """)).scalar()
        assert n == 0


def test_bypass_acl_rows_are_gone(migrated):
    with Session(migrated) as session:
        remaining = session.execute(text("""
            SELECT count(*) FROM resourceacl a JOIN accessgroup ag ON ag.id = a.accessgroup_id
            WHERE ag.group_name IN ('admin', 'read_only')
        """)).scalar()
    assert remaining == 0


def test_public_resources_gain_a_viewer_entry(migrated):
    with Session(migrated) as session:
        rows = dict(session.execute(text("""
            SELECT a.mfid, a.role FROM resourceacl a JOIN accessgroup ag ON ag.id = a.accessgroup_id
            WHERE ag.group_name = 'public'
        """)).all())
    assert rows == {"ds-pub": "viewer", "sm-pub": "viewer"}


def test_public_grant_cannot_exceed_viewer(migrated):
    """Standing in the public group is viewer, so no grant can raise it."""
    with Session(migrated) as session:
        gid = session.execute(text(
            "SELECT id FROM accessgroup WHERE group_name = 'public'")).scalar()
    outsider = Principal(user_id="0000-9999",
                         groups=(GroupMembership(gid, "public", Role.VIEWER),))
    assert effective_role(outsider, acl_for(migrated, "ds-pub")) is Role.VIEWER
    assert not has_permission(outsider, acl_for(migrated, "ds-pub"), Permission.EDIT)
    assert effective_role(outsider, acl_for(migrated, "ds-priv")) is None


def test_instrument_group_holds_only_the_service_account(migrated):
    with Session(migrated) as session:
        members = session.execute(text("""
            SELECT u.orcid FROM useraccessgrouplink l
            JOIN accessgroup ag ON ag.id = l.accessgroup_id
            JOIN "user" u ON u.id = l.user_id
            WHERE ag.group_name = 'xrd-1'
        """)).scalars().all()
    assert members == [SA]


def test_projects_became_resources(migrated):
    with Session(migrated) as session:
        kind = session.execute(text(
            "SELECT resource_type FROM idtype WHERE mfid = 'proj_x'")).scalar()
    assert kind == "project"


def test_one_owner_per_group_is_enforced(migrated):
    """A second owner in any group must be rejected by the database, not by
    application code that someone can forget to call."""
    from sqlalchemy.exc import IntegrityError
    with Session(migrated) as session:
        gid = session.execute(text(
            "SELECT id FROM accessgroup WHERE group_name = 'proj_x'")).scalar()
        uid = session.execute(text(
            'SELECT id FROM "user" WHERE orcid = :o'), {"o": STAFF}).scalar()
        with pytest.raises(IntegrityError):
            session.execute(text("""
                INSERT INTO useraccessgrouplink (user_id, accessgroup_id, role)
                VALUES (:u, :g, 'owner')
            """), {"u": uid, "g": gid})
            session.flush()


class TestLoadPrincipal:
    """The real auth-path query, against post-migration data. Route tests
    override authorize_user, so nothing else exercises this."""

    def _load(self, engine, orcid):
        from app.utils.auth_utils import load_principal
        with Session(engine) as session:
            return load_principal(session, orcid)

    def test_reads_standings_from_the_link_table(self, migrated):
        p = self._load(migrated, LEAD)
        assert p.role_in("proj_x") is Role.OWNER
        assert p.role_in(LEAD) is Role.OWNER

    def test_member_standing_is_editor_after_backfill(self, migrated):
        assert self._load(migrated, MEMBER).role_in("proj_x") is Role.EDITOR

    def test_platform_role_is_carried(self, migrated):
        assert self._load(migrated, PLATFORM_ADMIN).platform_role is PlatformRole.ADMIN
        assert self._load(migrated, MEMBER).platform_role is PlatformRole.NONE

    def test_service_account_flag_is_carried(self, migrated):
        assert self._load(migrated, SA).is_service_account is True
        assert self._load(migrated, LEAD).is_service_account is False

    def test_staff_lost_the_instrument_group(self, migrated):
        assert self._load(migrated, STAFF).role_in("xrd-1") is None

    def test_agrees_with_the_acl_rows(self, migrated):
        """The loaded principal must resolve the same effective role as one
        assembled by hand from the same tables."""
        for orcid in (LEAD, MEMBER, SA):
            for mfid in ("ds-pub", "ds-priv", "proj_x"):
                assert (effective_role(self._load(migrated, orcid), acl_for(migrated, mfid))
                        == effective_role(principal_for(migrated, orcid), acl_for(migrated, mfid)))

    def test_principal_is_hashable_so_it_can_be_cached(self, migrated):
        """authorize_user stores the result in a TTLCache shared across
        threads; a mutable value there would be a data race."""
        p = self._load(migrated, LEAD)
        assert hash(p) == hash(self._load(migrated, LEAD))


def test_backfill_is_idempotent(migrated):
    """A second run must change nothing — the plan calls for re-running against
    a fresh clone, and a partially-converted state must converge."""
    import migrations.backfill_permission_roles as backfill
    with Session(migrated) as session:
        assert backfill.backfill_standings(session) == 0
        assert backfill.backfill_acl_grants(session) == 0
        created, acl = backfill.create_project_resources(session)
        assert (created, acl) == (0, 0)
        assert backfill.migrate_public_flags(session) == (0, 0)
        session.rollback()
