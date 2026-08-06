"""Shared Postgres scaffolding for the migration and agreement tests.

Not a conftest: these helpers are imported explicitly by the two test modules
that need a real Postgres, so the hermetic SQLite suite never touches them.

Set CRUCIBLE_TEST_PG_URL to run those modules:

    docker run -d --rm --name crucible-pg-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=crucible_test \\
        -p 55432:5432 postgres:16-alpine

    CRUCIBLE_TEST_PG_URL=postgresql+psycopg2://postgres:test@127.0.0.1:55432/crucible_test \\
        .venv/bin/python -m pytest
"""
import os
from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

from app.models.core_models import (
    AccessGroup, Dataset, Instrument, Project, Resource, ResourceACL, Sample,
    User, UserAccessGroupLink, UserProjectLink,
)

PG_URL = os.getenv("CRUCIBLE_TEST_PG_URL")

REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_SQL = REPO_ROOT / "migrations" / "add_permission_roles.sql"

TABLES = [User, Project, Instrument, Resource, Dataset, Sample, AccessGroup,
          ResourceACL, UserAccessGroupLink, UserProjectLink]

LEAD, MEMBER, STAFF, PLATFORM_ADMIN, SA = (
    "0000-0001", "0000-0002", "0000-0003", "0000-0004", "0000-0009")

ALL_PRINCIPALS = [LEAD, MEMBER, STAFF, PLATFORM_ADMIN, SA]
ALL_RESOURCES = ["ds-pub", "ds-priv", "sm-pub", "proj_x"]


def fresh_schema(engine):
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    SQLModel.metadata.create_all(engine, tables=[m.__table__ for m in TABLES])


def drop_new_columns(engine):
    """Reproduce the pre-migration shape.

    create_all emits role/platform_role as VARCHAR because the models declare
    them as str for SQLite's sake. Production tables predate those columns, so
    they are dropped here — otherwise ADD COLUMN IF NOT EXISTS silently skips
    and the column never becomes the enum type.
    """
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE useraccessgrouplink DROP COLUMN IF EXISTS role"))
        conn.execute(text("ALTER TABLE resourceacl DROP COLUMN IF EXISTS role"))
        conn.execute(text('ALTER TABLE "user" DROP COLUMN IF EXISTS platform_role'))


def run_migration_sql(engine):
    """Execute the file as one script — it has a DO $$ ... $$ block with its own
    semicolons and its own BEGIN/COMMIT, so splitting on ';' corrupts it. This
    also matches how it runs in production (psql -f)."""
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute(MIGRATION_SQL.read_text())
    finally:
        raw.close()


def run_backfill(engine):
    import migrations.backfill_permission_roles as backfill

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


def group_ids_for(engine, orcid):
    """The ag_ids list the pre-role checks take."""
    with Session(engine) as session:
        return session.execute(text("""
            SELECT ag.id FROM useraccessgrouplink l
            JOIN accessgroup ag ON ag.id = l.accessgroup_id
            JOIN "user" u ON u.id = l.user_id
            WHERE u.orcid = :o
        """), {"o": orcid}).scalars().all()


def acl_for(engine, mfid):
    from app.utils.permissions import Role
    with Session(engine) as session:
        return [(gid, Role.from_db(role)) for gid, role in session.execute(
            text("SELECT accessgroup_id, role FROM resourceacl WHERE mfid = :m"),
            {"m": mfid}).all()]


def principal_for(engine, orcid):
    from app.utils.auth_utils import load_principal
    with Session(engine) as session:
        return load_principal(session, orcid)


def seed(session):
    """Representative pre-migration data: a project with a lead and a member,
    a beamline scientist sitting in an instrument group, a platform admin, an
    instrument service account, public and private resources, and a
    typo-created orphan group."""
    users = {
        LEAD: User(first_name="Lead", last_name="PI", email="a@x", unique_id=LEAD),
        MEMBER: User(first_name="Post", last_name="Doc", email="b@x", unique_id=MEMBER),
        STAFF: User(first_name="Beam", last_name="Sci", email="c@x", unique_id=STAFF),
        PLATFORM_ADMIN: User(first_name="Plat", last_name="Adm", email="d@x",
                             unique_id=PLATFORM_ADMIN),
        SA: User(first_name="", last_name="", email=None, unique_id=SA,
                 is_service_account=True),
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
    session.add(ResourceACL(mfid="ds-pub", accessgroup_id=groups[LEAD].id,
                            read=True, write=True))
    session.add(ResourceACL(mfid="ds-priv", accessgroup_id=groups[MEMBER].id,
                            read=True, write=True))
    session.add(ResourceACL(mfid="ds-pub", accessgroup_id=groups["xrd-1"].id,
                            read=True, write=True))
    session.add(ResourceACL(mfid="ds-priv", accessgroup_id=groups["xrd-typo"].id,
                            read=True, write=True))
    session.commit()


def build_migrated_engine():
    engine = create_engine(PG_URL)
    fresh_schema(engine)
    with Session(engine) as session:
        seed(session)
    drop_new_columns(engine)
    run_migration_sql(engine)
    run_backfill(engine)
    return engine
