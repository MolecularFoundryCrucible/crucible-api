import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError, DataError

VERSIONS = Path(__file__).resolve().parents[1] / "migrations/versions"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, VERSIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fails(connection, statement):
    with pytest.raises((IntegrityError, DataError)):
        with connection.begin_nested():
            connection.execute(text(statement))


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_postprocessing_and_join_request_migrations_roundtrip():
    postprocessing = _load("20261016_aa8eb05c643d_postprocessing_runs.py")
    join_requests = _load("20261016_c3f1d29a7b64_join_request_workflow.py")
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_workflows_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TYPE resource_role AS ENUM ('viewer', 'contributor', 'editor', 'admin', 'owner')"))
            connection.execute(text('CREATE TABLE "user" (id serial PRIMARY KEY, orcid text UNIQUE)'))
            connection.execute(text("CREATE TABLE dataset (id serial PRIMARY KEY, unique_id text UNIQUE)"))
            connection.execute(text(
                "CREATE TABLE accessgroupjoinrequest (id serial PRIMARY KEY, accessgroup_id integer NOT NULL, requester_id text NOT NULL, "
                "reason text, status text NOT NULL, request_time text, review_time text, reviewer_id text, reviewer_notes text)"
            ))
            connection.execute(text("INSERT INTO \"user\" (orcid) VALUES ('0000-0001')"))
            connection.execute(text("INSERT INTO dataset (unique_id) VALUES ('ds1')"))
            connection.execute(text(
                "INSERT INTO accessgroupjoinrequest (accessgroup_id, requester_id, status, request_time, review_time) VALUES "
                "(1, '0000-0001', 'approved', '2026-08-01T10:00:00.123456+00:00', '2026-08-02T09:30:00+00:00'), "
                "(1, '0000-0002', 'pending', NULL, NULL)"
            ))
            with Operations.context(MigrationContext.configure(connection)):
                postprocessing.upgrade()
                connection.execute(text("INSERT INTO postprocessingrun (dataset_mfid, pipeline, requested_by, requested_at) VALUES ('ds1', 'rga_analysis', '0000-0001', now())"))
                assert connection.execute(text("SELECT parameters::text, status FROM postprocessingrun")).one() == ("{}", "requested")
                _fails(connection, "INSERT INTO postprocessingrun (dataset_mfid, pipeline, status, requested_at) VALUES ('ds1', 'x', 'done', now())")
                _fails(connection, "INSERT INTO postprocessingrun (dataset_mfid, pipeline, requested_at) VALUES ('missing', 'x', now())")
                keyed = "INSERT INTO postprocessingrun (dataset_mfid, pipeline, requested_by, requested_at, idempotency_key) VALUES ('ds1', 'x', '0000-0001', now(), {})"
                connection.execute(text(keyed.format("'flow-1'")))
                _fails(connection, keyed.format("'flow-1'"))
                connection.execute(text(keyed.format("NULL")))
                connection.execute(text(keyed.format("NULL")))
                connection.execute(text("DELETE FROM \"user\""))
                assert connection.execute(text("SELECT count(*) FROM postprocessingrun WHERE requested_by IS NOT NULL")).scalar() == 0
                connection.execute(text("DELETE FROM dataset"))
                assert connection.execute(text("SELECT count(*) FROM postprocessingrun")).scalar() == 0

                join_requests.upgrade()
                columns = {column["name"]: column for column in inspect(connection).get_columns("accessgroupjoinrequest", schema=schema)}
                assert "TIMESTAMP" in str(columns["request_time"]["type"]) and columns["request_time"]["nullable"] is False
                reviewed = connection.execute(text("SELECT request_time, review_time FROM accessgroupjoinrequest WHERE status = 'approved'")).one()
                assert (reviewed[0].isoformat(), reviewed[1].isoformat()) == ("2026-08-01T10:00:00.123456+00:00", "2026-08-02T09:30:00+00:00")
                assert connection.execute(text("SELECT request_time IS NOT NULL FROM accessgroupjoinrequest WHERE status = 'pending'")).scalar()
                connection.execute(text("UPDATE accessgroupjoinrequest SET granted_role = 'editor' WHERE status = 'approved'"))
                _fails(connection, "UPDATE accessgroupjoinrequest SET status = 'cancelled'")
                _fails(connection, "UPDATE accessgroupjoinrequest SET granted_role = 'boss'")
                _fails(connection, "UPDATE accessgroupjoinrequest SET reason = repeat('x', 2001)")
                connection.execute(text("UPDATE accessgroupjoinrequest SET status = 'withdrawn' WHERE status = 'pending'"))
                assert "ix_agjr_group_status_time" in {index["name"] for index in inspect(connection).get_indexes("accessgroupjoinrequest", schema=schema)}

                join_requests.downgrade()
                postprocessing.downgrade()
                assert connection.execute(text("SELECT request_time FROM accessgroupjoinrequest WHERE status = 'approved'")).scalar().startswith("2026-08-01")
                assert "postprocessingrun" not in inspect(connection).get_table_names(schema=schema)
        finally:
            transaction.rollback()
    engine.dispose()


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_dropping_legacy_slug_foreign_keys_lets_projects_be_renamed():
    migration = _load("20261019_9d41c6a2e7f3_drop_legacy_slug_foreign_keys.py")
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_slug_fks_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE project (id serial PRIMARY KEY, project_id text UNIQUE, unique_id text UNIQUE)"))
            connection.execute(text("CREATE TABLE instrument (id serial PRIMARY KEY, unique_id text UNIQUE)"))
            connection.execute(text("CREATE TABLE dataset (id serial PRIMARY KEY, project_id text CONSTRAINT dataset_project_id_fkey REFERENCES project (project_id), "
                                    "instrument_id integer CONSTRAINT dataset_instrument_id_fkey REFERENCES instrument (id), project_mfid text)"))
            connection.execute(text("CREATE TABLE userprojectlink (user_id integer, project_id text CONSTRAINT userprojectlink_project_id_fkey REFERENCES project (project_id), PRIMARY KEY (user_id, project_id))"))
            connection.execute(text("INSERT INTO project (project_id, unique_id) VALUES ('old-slug', 'p1')"))
            connection.execute(text("INSERT INTO instrument (unique_id) VALUES ('i1')"))
            connection.execute(text("INSERT INTO dataset (project_id, instrument_id, project_mfid) VALUES ('old-slug', 1, 'p1')"))
            connection.execute(text("INSERT INTO userprojectlink VALUES (1, 'old-slug')"))
            _fails(connection, "UPDATE project SET project_id = 'new-slug'")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                connection.execute(text("UPDATE project SET project_id = 'new-slug'"))
                connection.execute(text("DELETE FROM instrument"))
                assert {fk["name"] for table in ("dataset", "userprojectlink") for fk in inspect(connection).get_foreign_keys(table, schema=schema)} == set()
                connection.execute(text("UPDATE project SET project_id = 'old-slug'"))
                connection.execute(text("INSERT INTO instrument (id, unique_id) VALUES (1, 'i1')"))
                migration.downgrade()
                assert len(inspect(connection).get_foreign_keys("dataset", schema=schema)) == 2
        finally:
            transaction.rollback()
    engine.dispose()


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
@pytest.mark.parametrize("bad_row", [None, "('/abs', 'globus', 1)", "('dup', 'globus', 1), ('dup', 'globus', 1)"])
def test_non_gcs_filename_identity_migration(bad_row):
    migration = _load("20261020_b8f2d6e41c07_non_gcs_filename_identity.py")
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_filename_identity_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE associatedfile (id serial PRIMARY KEY, filename varchar NOT NULL, storage_backend varchar NOT NULL, dataset_id integer NOT NULL)"))
            connection.execute(text("INSERT INTO associatedfile (filename, storage_backend, dataset_id) VALUES ('/home/x/scan.tif', 'gcs', 1), ('/home/x/scan.tif', 'gcs', 1), ('ok/file', 'globus', 1)"))
            if bad_row:
                connection.execute(text(f"INSERT INTO associatedfile (filename, storage_backend, dataset_id) VALUES {bad_row}"))
            with Operations.context(MigrationContext.configure(connection)):
                if bad_row:
                    with pytest.raises(RuntimeError):
                        migration.upgrade()
                    return
                migration.upgrade()
                for statement in ("INSERT INTO associatedfile (filename, storage_backend, dataset_id) VALUES ('ok/file', 'globus', 1)",
                                  "INSERT INTO associatedfile (filename, storage_backend, dataset_id) VALUES ('../x', 'globus', 2)",
                                  "INSERT INTO associatedfile (filename, storage_backend, dataset_id) VALUES (E'a\\x01b', 'globus', 2)"):
                    _fails(connection, statement)
                connection.execute(text("INSERT INTO associatedfile (filename, storage_backend, dataset_id) VALUES ('/home/x/scan.tif', 'gcs', 1)"))
                migration.downgrade()
                connection.execute(text("INSERT INTO associatedfile (filename, storage_backend, dataset_id) VALUES ('ok/file', 'globus', 1)"))
        finally:
            transaction.rollback()
    engine.dispose()
