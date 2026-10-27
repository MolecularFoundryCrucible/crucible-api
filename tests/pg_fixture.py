"""Shared Postgres scaffolding for tests that need a real server.

Not a conftest: imported explicitly by the modules that need it, so the hermetic SQLite suite never
touches it. Set CRUCIBLE_TEST_PG_URL to a database on a pgvector server to run them:

    docker run -d --rm --name crucible-pg-test \\
        -e POSTGRES_PASSWORD=test -e POSTGRES_DB=crucible_test \\
        -p 55432:5432 pgvector/pgvector:0.8.1-pg17

    CRUCIBLE_TEST_PG_URL=postgresql+psycopg2://postgres:test@127.0.0.1:55432/crucible_test \\
        .venv/bin/python -m pytest
"""
import os
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlmodel import Session, create_engine

from app.models.core_models import AccessGroup, Dataset, Project, Resource, ResourceACL, User, UserAccessGroupLink

PG_URL = os.getenv("CRUCIBLE_TEST_PG_URL")

LEAD, MEMBER, STAFF, PLATFORM_ADMIN, SA = (
    "0000-0001", "0000-0002", "0000-0003", "0000-0004", "0000-0009")


@contextmanager
def throwaway_database():
    """An engine on a new, empty database on the CRUCIBLE_TEST_PG_URL server, dropped afterwards."""
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    name = "crucible_test_" + uuid4().hex[:12]
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(make_url(PG_URL).set(database=name))
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        admin.dispose()


def _add_one_at_a_time(session, rows):
    # user.platform_role and accessgroup.group_type are PostgreSQL enums but plain strings in the
    # models, and a multi-row insert casts every value to the model's type, which PostgreSQL refuses.
    for row in rows:
        session.add(row)
        session.flush()


def seed_permissions(engine):
    """A project with a lead and a member, a beamline scientist outside it, a platform admin, and an
    instrument service account, with a public and a private dataset:

    - proj_x: LEAD owner, MEMBER editor; it grants admin on both datasets.
    - ds-pub (LEAD's, public): the instrument group xrd-1 (SA, contributor) grants contributor.
    - ds-priv (MEMBER's, private): a typo group nobody is in grants editor.
    """
    with Session(engine) as session:
        users = {
            LEAD: User(first_name="Lead", last_name="PI", email="a@x", unique_id=LEAD),
            MEMBER: User(first_name="Post", last_name="Doc", email="b@x", unique_id=MEMBER),
            STAFF: User(first_name="Beam", last_name="Sci", email="c@x", unique_id=STAFF),
            PLATFORM_ADMIN: User(first_name="Plat", last_name="Adm", email="d@x", unique_id=PLATFORM_ADMIN, platform_role="admin"),
            SA: User(first_name="", last_name="", email=None, unique_id=SA, is_service_account=True),
        }
        _add_one_at_a_time(session, users.values())
        session.add(Project(unique_id="proj_x", project_id="proj_x", organization="LBNL", status="active", title="X", project_lead_orcid=LEAD))
        groups = {orcid: AccessGroup(group_name=orcid, group_type="identity") for orcid in users}
        groups.update({
            "proj_x": AccessGroup(group_name="proj_x", group_type="project"),
            "xrd-1": AccessGroup(group_name="xrd-1", group_type="instrument"),
            "xrd-typo": AccessGroup(group_name="xrd-typo", group_type="instrument"),
        })
        _add_one_at_a_time(session, groups.values())
        memberships = [(orcid, orcid, "owner") for orcid in users] + [
            (LEAD, "proj_x", "owner"), (MEMBER, "proj_x", "editor"), (SA, "xrd-1", "contributor"),
        ]
        session.add_all([UserAccessGroupLink(user_id=users[orcid].id, accessgroup_id=groups[name].id, role=role) for orcid, name, role in memberships])
        public_id = session.execute(text("SELECT id FROM accessgroup WHERE group_name = 'public'")).scalar_one()
        for mfid, owner, public in (("ds-pub", LEAD, True), ("ds-priv", MEMBER, False)):
            session.add(Resource(mfid=mfid, resource_type="dataset", public=public))
            session.add(Dataset(unique_id=mfid, dataset_name=mfid, public=public, project_mfid="proj_x", owner_orcid=owner))
        session.flush()
        grants = [
            ("ds-pub", groups[LEAD].id, "owner"), ("ds-pub", groups["proj_x"].id, "admin"),
            ("ds-pub", groups["xrd-1"].id, "contributor"), ("ds-pub", public_id, "viewer"),
            ("ds-priv", groups[MEMBER].id, "owner"), ("ds-priv", groups["proj_x"].id, "admin"),
            ("ds-priv", groups["xrd-typo"].id, "editor"),
        ]
        session.add_all([ResourceACL(mfid=mfid, accessgroup_id=group_id, read=True, write=role != "viewer", permission=role) for mfid, group_id, role in grants])
        session.commit()


def principal_for(engine, orcid):
    from app.utils import auth_utils

    # The public group's id is cached per process; look it up in this database, then restore.
    cached, auth_utils._PUBLIC_GROUP_ID = auth_utils._PUBLIC_GROUP_ID, auth_utils._UNSET
    try:
        with Session(engine) as session:
            return auth_utils.load_principal(session, orcid)
    finally:
        auth_utils._PUBLIC_GROUP_ID = cached
