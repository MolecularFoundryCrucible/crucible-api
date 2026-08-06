"""End-to-end test of the phase 1 permission migration against a real Postgres.

The migration is Postgres-specific — enum types, casts, CTEs in UPDATE,
DELETE ... USING — so SQLite cannot validate it and the pure matrix in
test_permissions.py cannot either. This runs the actual SQL file and the actual
backfill against a throwaway database, then asserts on effective roles computed
from the resulting rows.

Skipped unless CRUCIBLE_TEST_PG_URL is set. To run:

    docker run -d --rm --name crucible-pg-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=crucible_test \\
        -p 55432:5432 postgres:16-alpine

    CRUCIBLE_TEST_PG_URL=postgresql+psycopg2://postgres:test@127.0.0.1:55432/crucible_test \\
        .venv/bin/python -m pytest tests/test_migration_permission_roles.py
"""
import os
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlmodel import SQLModel, Session, create_engine

from app.models.core_models import (
    AccessGroup, Dataset, Instrument, Project, Resource, ResourceACL, Sample,
    User, UserAccessGroupLink, UserProjectLink,
)
from app.utils.permissions import (
    GroupMembership, Permission, PlatformRole, Principal, Role,
    effective_role, has_permission,
)

PG_URL = os.getenv("CRUCIBLE_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="CRUCIBLE_TEST_PG_URL not set")

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_SQL = REPO_ROOT / "migrations" / "add_permission_roles.sql"

TABLES = [User, Project, Instrument, Resource, Dataset, Sample, AccessGroup,
          ResourceACL, UserAccessGroupLink, UserProjectLink]

LEAD, MEMBER, STAFF, PLATFORM_ADMIN, SA = (
    "0000-0001", "0000-0002", "0000-0003", "0000-0004", "0000-0009")


def _seed(session):
    users = {
        LEAD: User(first_name="Lead", last_name="PI", email="a@x", unique_id=LEAD),
        MEMBER: User(first_name="Post", last_name="Doc", email="b@x", unique_id=MEMBER),
        STAFF: User(first_name="Beam", last_name="Sci", email="c@x", unique_id=STAFF),
        PLATFORM_ADMIN: User(first_name="Plat", last_name="Adm", email="d@x", unique_id=PLATFORM_ADMIN),
        SA: User(first_name="", last_name="", email=None, unique_id=SA, is_service_account=True),
    }
    for u in users.values():
        session.add(u)
    session.add(Project(project_id="proj_x", organization="LBNL", status="active",
                        title="X", project_lead_orcid=LEAD))
    session.add(Instrument(unique_id="inst-1", instrument_name="XRD-1",
                           owner=LEAD, location="bldg 67"))
    session.flush()

    groups = {}
    for name in ("proj_x", LEAD, MEMBER, STAFF, PLATFORM_ADMIN, SA,
                 "xrd-1", "admin", "read_only", "xrd-typo"):
        g = AccessGroup(group_name=name)
        session.add(g)
        groups[name] = g
    session.flush()

    memberships = [
        (LEAD, "proj_x"), (MEMBER, "proj_x"),
        (LEAD, LEAD), (MEMBER, MEMBER), (STAFF, STAFF),
        (PLATFORM_ADMIN, PLATFORM_ADMIN), (PLATFORM_ADMIN, "admin"),
        (SA, SA), (SA, "xrd-1"),
        (STAFF, "xrd-1"),  # a human in an instrument group; must be removed
    ]
    for orcid, group_name in memberships:
        session.add(UserAccessGroupLink(user_id=users[orcid].id,
                                        accessgroup_id=groups[group_name].id))

    session.add(Resource(mfid="ds-pub", resource_type="dataset", public=True))
    session.add(Dataset(unique_id="ds-pub", dataset_name="public", public=True,
                        project_id="proj_x", owner_orcid=LEAD))
    session.add(Resource(mfid="ds-priv", resource_type="dataset", public=False))
    session.add(Dataset(unique_id="ds-priv", dataset_name="private", public=False,
                        project_id="proj_x", owner_orcid=MEMBER))
    session.add(Resource(mfid="sm-pub", resource_type="sample", public=True))
    session.add(Sample(unique_id="sm-pub", sample_name="public", project_id="proj_x"))

    for mfid in ("ds-pub", "ds-priv", "sm-pub"):
        for name in ("proj_x", "admin", "read_only"):
            session.add(ResourceACL(mfid=mfid, accessgroup_id=groups[name].id,
                                    read=True, write=(name != "read_only")))
    session.add(ResourceACL(mfid="ds-pub", accessgroup_id=groups[LEAD].id, read=True, write=True))
    session.add(ResourceACL(mfid="ds-priv", accessgroup_id=groups[MEMBER].id, read=True, write=True))
    session.add(ResourceACL(mfid="ds-pub", accessgroup_id=groups["xrd-1"].id, read=True, write=True))
    session.add(ResourceACL(mfid="ds-priv", accessgroup_id=groups["xrd-typo"].id, read=True, write=True))
    session.commit()


@pytest.fixture(scope="module")
def migrated():
    """Fresh schema, real migration SQL, real backfill. Module-scoped: the
    migration is the thing under test, so it runs once and every assertion
    reads the result."""
    import migrations.backfill_permission_roles as backfill

    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in TABLES])

    with Session(engine) as session:
        _seed(session)

    # create_all emits the new columns as VARCHAR, because the models declare
    # them as str so SQLite-backed tests still work. Production's tables predate
    # those columns entirely, so drop them here — after seeding, which needs the
    # ORM — to reproduce the state the migration actually runs against.
    # Otherwise ADD COLUMN IF NOT EXISTS silently skips and the column never
    # becomes the enum type.
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE useraccessgrouplink DROP COLUMN IF EXISTS role"))
        conn.execute(text("ALTER TABLE resourceacl DROP COLUMN IF EXISTS role"))
        conn.execute(text('ALTER TABLE "user" DROP COLUMN IF EXISTS platform_role'))

    # Execute the file as one script rather than splitting on ';' — it contains
    # a DO $$ ... $$ block whose body has its own semicolons, and its own
    # BEGIN/COMMIT. This is also closer to how it runs in production (psql -f).
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute(MIGRATION_SQL.read_text())
    finally:
        raw.close()

    with Session(engine) as session:
        backfill.backfill_standings(session)
        backfill.create_project_resources(session)
        backfill.drop_bypass_acl_rows(session)
        backfill.backfill_acl_grants(session)
        backfill.migrate_public_flags(session)
        backfill.set_platform_admins(session)
        backfill.scope_instrument_groups(session)
        nulls_standing, nulls_grant, dupes = backfill.verify(session)
        assert (nulls_standing, nulls_grant, dupes) == (0, 0, [])
        backfill.create_owner_index(session)
        session.commit()

    return engine


def principal_for(engine, orcid) -> Principal:
    with Session(engine) as session:
        platform_role = session.execute(
            text('SELECT platform_role FROM "user" WHERE orcid = :o'), {"o": orcid}).scalar()
        rows = session.execute(text("""
            SELECT ag.id, ag.group_name, l.role
            FROM useraccessgrouplink l
            JOIN accessgroup ag ON ag.id = l.accessgroup_id
            JOIN "user" u ON u.id = l.user_id
            WHERE u.orcid = :o
        """), {"o": orcid}).all()
    return Principal(
        user_id=orcid,
        platform_role=PlatformRole(platform_role),
        groups=tuple(GroupMembership(gid, name, Role.from_db(role)) for gid, name, role in rows),
    )


def acl_for(engine, mfid):
    with Session(engine) as session:
        return [(gid, Role.from_db(role)) for gid, role in session.execute(
            text("SELECT accessgroup_id, role FROM resourceacl WHERE mfid = :m"),
            {"m": mfid}).all()]


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
    # Facility staff lose instrument-derived access entirely.
    (STAFF, "ds-pub", None),
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
