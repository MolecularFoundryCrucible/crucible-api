import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, select

from app.api.routes import deletion as deletion_routes
from app.tables import DeletionAuditLog, DeletionRequest, User
from app.models.deletion import DeletionAuditScope, DeletionRequestScope
from app.models.query_params import SortDirection
from app.utils.permissions import PlatformRole
from tests.people import person_principal

OLD = "0000-0002-1825-0097"
NEW = "0tmnyhfztdwc1000w2kspzsnh9"
GONE = "0000-0009-0000-0009"


def _audit(auth_res, **overrides):
    params = {"scope": DeletionAuditScope.ALL, "resource_id": None, "resource_type": None, "project_id": None, "project_mfid": None,
              "requester_id": None, "reviewer_id": None, "direction": SortDirection.DESC, "limit": 100, "cursor": None, "offset": 0}
    return deletion_routes.list_deletion_audit(auth_res=auth_res, **{**params, **overrides})


def _person_with_history(db_engine):
    with Session(db_engine) as session:
        user = User(unique_id=OLD, first_name="Ada", last_name="L", email=None, username="ada")
        session.add(user)
        session.flush()
        request = DeletionRequest(resource_id="gone-dataset", requester_id=OLD, requester_user_id=user.id, status="rejected")
        session.add(request)
        session.add(DeletionAuditLog(resource_id="mine", resource_type="dataset", requester_id=OLD, requester_user_id=user.id,
                                     reviewer_id=OLD, reviewer_user_id=user.id, deleted_at="2026-11-01T00:00:00+00:00"))
        session.add(DeletionAuditLog(resource_id="old-unlinked", resource_type="dataset", requester_id=GONE, reviewer_id=GONE,
                                     deleted_at="2026-10-01T00:00:00+00:00"))
        session.commit()
        user.unique_id = NEW
        session.commit()
        return request.id


def test_my_deletion_history_survives_a_unique_id_change(db_engine):
    _person_with_history(db_engine)
    me = person_principal(user_id=NEW)
    mine = _audit(me, scope=DeletionAuditScope.SUBMITTED)
    assert [(item.resource_id, item.requester_id) for item in mine.items] == [("mine", NEW)]
    assert _audit(me, scope=DeletionAuditScope.SUBMITTED, requester_id=NEW).total == 1
    audit_id = mine.items[0].id
    assert deletion_routes.get_deletion_audit(auth_res=me, audit_id=audit_id).requester_id == NEW


def test_my_deletion_requests_survive_a_unique_id_change(db_engine):
    request_id = _person_with_history(db_engine)
    me = person_principal(user_id=NEW)
    listed = deletion_routes.list_deletion_requests(me, scope=DeletionRequestScope.SUBMITTED, status=None, limit=100, offset=0)
    assert [(item.id, item.requester_id) for item in listed.items] == [(request_id, NEW)]
    assert deletion_routes.get_deletion_request(auth_res=me, request_id=request_id).requester_id == NEW


def test_unlinked_audit_rows_show_and_filter_by_their_stored_text(db_engine):
    _person_with_history(db_engine)
    admin = person_principal(user_id="platform-admin", platform_role=PlatformRole.ADMIN)
    by_text = _audit(admin, requester_id=GONE)
    assert [(item.resource_id, item.requester_id, item.requester) for item in by_text.items] == [("old-unlinked", GONE, None)]
    assert _audit(admin, reviewer_id=GONE).total == 1
    assert _audit(admin, requester_id=OLD).total == 0
    assert _audit(admin, requester_id=NEW).total == 1


def test_a_stranger_cannot_read_an_unlinked_audit_row(db_engine):
    _person_with_history(db_engine)
    with Session(db_engine) as session:
        audit_id = session.exec(select(DeletionAuditLog.id).where(DeletionAuditLog.resource_id == "old-unlinked")).one()
    with pytest.raises(Exception) as caught:
        deletion_routes.get_deletion_audit(auth_res=person_principal(user_id="stranger"), audit_id=audit_id)
    assert caught.value.status_code == 403


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_deletion_user_id_migration_fills_matches_and_follows_old_style_writes():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261105_e61b3c9d4f05_deletion_user_id.py"
    spec = importlib.util.spec_from_file_location("deletion_user_id_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_deletion_user_id_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            run = lambda sql: connection.execute(text(sql))  # noqa: E731
            one = lambda sql: connection.execute(text(sql)).one()  # noqa: E731
            run(f'CREATE SCHEMA "{schema}"')
            run(f'SET LOCAL search_path TO "{schema}"')
            run('CREATE TABLE "user" (id serial PRIMARY KEY, orcid text UNIQUE)')
            run("CREATE TABLE deletionrequest (id serial PRIMARY KEY, requester_id text NOT NULL, reviewer_id text)")
            run("CREATE TABLE deletionauditlog (id serial PRIMARY KEY, requester_id text NOT NULL, reviewer_id text NOT NULL)")
            run(f"INSERT INTO \"user\" (orcid) VALUES ('{OLD}'), ('{NEW}')")
            ada, other = connection.execute(text('SELECT id FROM "user" ORDER BY id')).scalars().all()
            run(f"INSERT INTO deletionrequest (requester_id, reviewer_id) VALUES ('{OLD}', '{GONE}')")
            run(f"INSERT INTO deletionauditlog (requester_id, reviewer_id) VALUES ('{GONE}', '{OLD}')")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert one("SELECT requester_user_id, reviewer_user_id FROM deletionrequest") == (ada, None)
                assert one("SELECT requester_user_id, reviewer_user_id FROM deletionauditlog") == (None, ada)
                run(f"UPDATE deletionrequest SET reviewer_id = '{NEW}'")
                assert one("SELECT reviewer_user_id FROM deletionrequest") == (other,)
                run(f"INSERT INTO deletionauditlog (requester_id, reviewer_id) VALUES ('{NEW}', '{GONE}')")
                assert one(f"SELECT requester_user_id, reviewer_user_id FROM deletionauditlog WHERE requester_id = '{NEW}'") == (other, None)
                migration.downgrade()
                for table, _, id_column in migration.COLUMNS:
                    assert id_column not in {c["name"] for c in inspect(connection).get_columns(table, schema=schema)}
                assert one("SELECT count(*) FROM pg_proc WHERE proname LIKE 'fill_deletion%'") == (0,)
        finally:
            transaction.rollback()
    engine.dispose()


def test_a_caller_without_an_internal_id_owns_no_unlinked_rows(db_engine):
    from app.utils.permissions import Principal

    _person_with_history(db_engine)
    nobody = Principal(user_id=GONE)
    assert _audit(nobody, scope=DeletionAuditScope.SUBMITTED).total == 0
    with Session(db_engine) as session:
        audit_id = session.exec(select(DeletionAuditLog.id).where(DeletionAuditLog.resource_id == "old-unlinked")).one()
    with pytest.raises(Exception) as caught:
        deletion_routes.get_deletion_audit(auth_res=nobody, audit_id=audit_id)
    assert caught.value.status_code == 403
