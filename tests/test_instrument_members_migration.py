import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlmodel import Session, select

from app.cli import db
from app.services.instrument_members import datasets_without_id_record, grant_problems, repair_grants
from app.tables import AccessGroup, Dataset, Instrument, Resource, ResourceACL, User, UserAccessGroupLink

PG_URL = os.getenv("CRUCIBLE_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="requires isolated test PostgreSQL")


def _alembic(engine, action, revision):
    config = Config(str(db.REPO_ROOT / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        action(config, revision)


def _seed(engine):
    with Session(engine) as session:
        def add(row):
            session.add(row)
            session.flush()
            return row

        people = {}
        for unique_id, is_sa in (("owner", False), ("editor", False), ("sa", True), ("contrib", False), ("pm_admin", False), ("pm_viewer", False)):
            user = add(User(unique_id=unique_id, first_name="T", last_name=unique_id, is_service_account=is_sa))
            group = add(AccessGroup(group_name=unique_id, group_type="identity"))
            add(UserAccessGroupLink(user_id=user.id, accessgroup_id=group.id, role="owner"))
            people[unique_id] = (user.id, group.id)
        operators = {}
        for mfid in ("inst-1", "inst-2"):
            add(Resource(mfid=mfid, resource_type="instrument"))
            add(Instrument(unique_id=mfid, instrument_id=mfid, instrument_name=mfid, location="b30"))
            operators[mfid] = add(AccessGroup(group_name=mfid, group_type="instrument")).id
        for mfid, unique_id, role in (("inst-1", "owner", "owner"), ("inst-1", "editor", "editor"), ("inst-1", "sa", "viewer"),
                                      ("inst-1", "contrib", "contributor"), ("inst-1", "pm_viewer", "editor"), ("inst-2", "sa", "owner")):
            add(ResourceACL(mfid=mfid, accessgroup_id=people[unique_id][1], read=True, write=role != "viewer", permission=role))
        project = add(AccessGroup(group_name="proj-x", group_type="project")).id
        for unique_id, role in (("pm_admin", "admin"), ("pm_viewer", "viewer"), ("sa", "editor")):
            add(UserAccessGroupLink(user_id=people[unique_id][0], accessgroup_id=project, role=role))
        for mfid, role in (("inst-1", "viewer"), ("inst-2", "editor")):
            add(ResourceACL(mfid=mfid, accessgroup_id=project, read=True, write=role != "viewer", permission=role))
        for mfid, instrument in (("ds-1", "inst-1"), ("ds-2", "inst-1"), ("ds-3", "inst-2")):
            add(Resource(mfid=mfid, resource_type="dataset"))
            add(Dataset(unique_id=mfid, dataset_name=mfid, instrument_mfid=instrument))
        add(Dataset(unique_id="no-record", dataset_name="no-record", instrument_mfid="inst-1"))
        add(Resource(mfid="gone", resource_type="dataset"))
        for mfid, operator in (("ds-1", "inst-1"), ("ds-3", "inst-1"), ("gone", "inst-1")):
            add(ResourceACL(mfid=mfid, accessgroup_id=operators[operator], read=True, write=True, permission="contributor"))
        session.commit()


def _grants(connection, sql):
    return sorted(tuple(row) for row in connection.execute(text(sql)).all())


GRANTS = """SELECT acl.mfid, g.group_name, g.group_type, acl.permission::text FROM resourceacl acl
            JOIN accessgroup g ON g.id = acl.accessgroup_id WHERE acl.mfid IN ('inst-1', 'inst-2', 'ds-1', 'ds-2', 'ds-3', 'gone')"""
MEMBERS = """SELECT g.group_name, u.orcid, link.role::text FROM useraccessgrouplink link
             JOIN accessgroup g ON g.id = link.accessgroup_id JOIN "user" u ON u.id = link.user_id
             WHERE g.group_type = 'instrument_member'"""


def test_upgrade_moves_maintainers_into_members_and_downgrade_restores_them():
    from tests.pg_fixture import throwaway_database

    with throwaway_database() as engine:
        db._apply_baseline(engine)
        _alembic(engine, command.upgrade, "c41d7e9a52b6")
        _seed(engine)

        _alembic(engine, command.upgrade, "head")
        with engine.connect() as connection:
            assert _grants(connection, GRANTS) == sorted([
                ("inst-1", "inst-1:members", "instrument_member", "owner"),
                ("inst-2", "inst-2:members", "instrument_member", "owner"),
                ("ds-1", "inst-1", "instrument", "contributor"),
                ("ds-1", "inst-1:members", "instrument_member", "viewer"),
                ("ds-2", "inst-1", "instrument", "contributor"),
                ("ds-2", "inst-1:members", "instrument_member", "viewer"),
                ("ds-3", "inst-2", "instrument", "contributor"),
                ("ds-3", "inst-2:members", "instrument_member", "viewer"),
            ])
            assert _grants(connection, MEMBERS) == sorted([
                ("inst-1:members", "owner", "owner"), ("inst-1:members", "editor", "editor"), ("inst-1:members", "contrib", "editor"),
                ("inst-1:members", "pm_admin", "viewer"), ("inst-1:members", "pm_viewer", "editor"),
                ("inst-2:members", "pm_admin", "owner"), ("inst-2:members", "pm_viewer", "viewer"),
            ])
            assert connection.execute(text(
                "SELECT u.orcid, i.owner FROM instrument i JOIN \"user\" u ON u.id = i.owner_user_id WHERE i.unique_id = 'inst-2'")).one() == ("pm_admin", "pm_admin")
            read_write = connection.execute(text(
                "SELECT read, write FROM resourceacl acl JOIN accessgroup g ON g.id = acl.accessgroup_id "
                "WHERE acl.mfid = 'ds-2' AND g.group_type = 'instrument_member'")).one()
            assert tuple(read_write) == (True, False)
        with Session(engine) as session:
            assert set(grant_problems(session).values()) == {0}
            assert datasets_without_id_record(session) == 1

        _alembic(engine, command.downgrade, "3b8e5d1f7c24")
        with engine.connect() as connection:
            assert _grants(connection, MEMBERS) == []
            record = _grants(connection, GRANTS.replace("('inst-1', 'inst-2', 'ds-1', 'ds-2', 'ds-3', 'gone')", "('inst-1', 'inst-2')"))
            assert record == sorted([
                ("inst-1", "owner", "identity", "owner"), ("inst-1", "editor", "identity", "editor"), ("inst-1", "contrib", "identity", "editor"),
                ("inst-1", "pm_admin", "identity", "viewer"), ("inst-1", "pm_viewer", "identity", "editor"),
                ("inst-2", "pm_admin", "identity", "owner"), ("inst-2", "pm_viewer", "identity", "viewer"),
            ])
            assert connection.execute(text("SELECT count(*) FROM accessgroup WHERE group_type = 'instrument_member'")).scalar() == 0


def test_repair_converts_rows_written_by_the_previous_release_after_the_migration():
    from tests.pg_fixture import throwaway_database

    with throwaway_database() as engine:
        db._apply_baseline(engine)
        _alembic(engine, command.upgrade, "c41d7e9a52b6")
        _seed(engine)
        _alembic(engine, command.upgrade, "head")
        with Session(engine) as session:
            def add(row):
                session.add(row)
                session.flush()
                return row

            owner_group = session.exec(select(AccessGroup.id).where(AccessGroup.group_name == "owner")).one()
            add(Resource(mfid="inst-3", resource_type="instrument"))
            add(Instrument(unique_id="inst-3", instrument_id="inst-3", instrument_name="inst-3", location="b30"))
            operator = add(AccessGroup(group_name="inst-3", group_type="instrument")).id
            add(ResourceACL(mfid="inst-3", accessgroup_id=owner_group, read=True, write=True, permission="owner"))
            add(Resource(mfid="ds-4", resource_type="dataset"))
            add(Dataset(unique_id="ds-4", dataset_name="ds-4", instrument_mfid="inst-3"))
            add(ResourceACL(mfid="ds-4", accessgroup_id=operator, read=True, write=True, permission="contributor"))
            session.execute(text("UPDATE dataset SET instrument_mfid = 'inst-2' WHERE unique_id = 'ds-1'"))
            session.commit()
            assert any(grant_problems(session).values())

            repair_grants(session)
            session.commit()
            assert set(grant_problems(session).values()) == {0}
        with engine.connect() as connection:
            assert ("inst-3:members", "owner", "owner") in _grants(connection, MEMBERS)
            assert ("ds-1", "inst-1:members", "instrument_member", "viewer") not in _grants(connection, GRANTS)
