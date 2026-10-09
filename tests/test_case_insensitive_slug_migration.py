"""Round trip of the case-insensitive slug index revision against an isolated local PostgreSQL.

Set SLUG_INDEX_TEST_DATABASE_URL to a local database named crucible_slug_index_test to run it.
"""
import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError


REVISION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/20261005_b7e2d9c4a1f3_case_insensitive_slug_indexes.py"


def _revision():
    spec = importlib.util.spec_from_file_location("case_insensitive_slug_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pg_engine():
    value = os.getenv("SLUG_INDEX_TEST_DATABASE_URL")
    if not value:
        pytest.skip("SLUG_INDEX_TEST_DATABASE_URL requires an isolated local PostgreSQL database")
    url = make_url(value)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "crucible_slug_index_test":
        pytest.fail("Use a local database named crucible_slug_index_test")
    schema = "slug_index_test_" + uuid.uuid4().hex
    admin = create_engine(value)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(value, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE project (id SERIAL PRIMARY KEY, project_id VARCHAR UNIQUE)")
            connection.exec_driver_sql("CREATE TABLE instrument (id SERIAL PRIMARY KEY, instrument_id VARCHAR UNIQUE)")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _run(engine, step):
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"transactional_ddl": True})
        with Operations.context(context), context.begin_transaction():
            step()


def test_upgrade_enforces_case_insensitive_slugs_and_downgrade_removes_it(pg_engine):
    revision = _revision()
    with pg_engine.begin() as connection:
        connection.execute(text("INSERT INTO project (project_id) VALUES ('Proj-A')"))
        connection.execute(text("INSERT INTO instrument (instrument_id) VALUES ('XRD-1'), (NULL), (NULL)"))

    _run(pg_engine, revision.upgrade)
    with pg_engine.connect() as connection:
        for name, table, _ in revision.INDEXES:
            assert name in {index["name"] for index in inspect(connection).get_indexes(table)}
            assert connection.execute(text(
                "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
            ), {"name": name}).scalar_one()
    for statement in ("INSERT INTO project (project_id) VALUES ('proj-a')",
                      "INSERT INTO instrument (instrument_id) VALUES ('xrd-1')"):
        with pytest.raises(IntegrityError), pg_engine.begin() as connection:
            connection.execute(text(statement))

    _run(pg_engine, revision.downgrade)
    with pg_engine.begin() as connection:
        connection.execute(text("INSERT INTO project (project_id) VALUES ('proj-a')"))


def test_upgrade_refuses_existing_case_collisions(pg_engine):
    revision = _revision()
    with pg_engine.begin() as connection:
        connection.execute(text("INSERT INTO project (project_id) VALUES ('Proj-A'), ('proj-a')"))

    with pytest.raises(RuntimeError, match="differing only by case"):
        _run(pg_engine, revision.upgrade)
    with pg_engine.connect() as connection:
        assert "ux_project_project_id_lower" not in {index["name"] for index in inspect(connection).get_indexes("project")}
