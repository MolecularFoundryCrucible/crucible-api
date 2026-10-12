"""Round trip of the unique user email index revision against an isolated local PostgreSQL.

Set EMAIL_INDEX_TEST_DATABASE_URL to a local database named crucible_email_index_test to run it.
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


REVISION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/20261012_e5a1c8d93f27_unique_user_email.py"


def _revision():
    spec = importlib.util.spec_from_file_location("unique_user_email_revision", REVISION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def pg_engine():
    value = os.getenv("EMAIL_INDEX_TEST_DATABASE_URL")
    if not value:
        pytest.skip("EMAIL_INDEX_TEST_DATABASE_URL requires an isolated local PostgreSQL database")
    url = make_url(value)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "crucible_email_index_test":
        pytest.fail("Use a local database named crucible_email_index_test")
    schema = "email_index_test_" + uuid.uuid4().hex
    admin = create_engine(value)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(value, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE "user" (id SERIAL PRIMARY KEY, orcid VARCHAR UNIQUE, email VARCHAR)')
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


def test_upgrade_enforces_unique_emails_and_downgrade_removes_it(pg_engine):
    revision = _revision()
    with pg_engine.begin() as connection:
        connection.execute(text("""INSERT INTO "user" (orcid, email) VALUES ('a', 'x@lbl.gov'), ('b', NULL), ('c', NULL)"""))

    _run(pg_engine, revision.upgrade)
    with pg_engine.connect() as connection:
        assert "ux_user_email_lower" in {index["name"] for index in inspect(connection).get_indexes("user")}
        assert connection.execute(text("SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass('ux_user_email_lower')")).scalar_one()
    with pg_engine.begin() as connection:
        connection.execute(text("""INSERT INTO "user" (orcid, email) VALUES ('d', NULL)"""))
    with pytest.raises(IntegrityError), pg_engine.begin() as connection:
        connection.execute(text("""INSERT INTO "user" (orcid, email) VALUES ('e', 'X@LBL.gov')"""))

    _run(pg_engine, revision.downgrade)
    with pg_engine.begin() as connection:
        connection.execute(text("""INSERT INTO "user" (orcid, email) VALUES ('e', 'X@LBL.gov')"""))


def test_upgrade_refuses_shared_emails(pg_engine):
    revision = _revision()
    with pg_engine.begin() as connection:
        connection.execute(text("""INSERT INTO "user" (orcid, email) VALUES ('a', 'x@lbl.gov'), ('b', 'X@lbl.gov')"""))

    with pytest.raises(RuntimeError, match="shared by more than one user"):
        _run(pg_engine, revision.upgrade)
    with pg_engine.connect() as connection:
        assert "ux_user_email_lower" not in {index["name"] for index in inspect(connection).get_indexes("user")}
