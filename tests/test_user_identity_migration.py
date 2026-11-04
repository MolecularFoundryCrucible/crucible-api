import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_migration_adds_an_empty_table_and_downgrades():
    path = next((ROOT / "migrations/versions").glob("*_user_identity.py"))
    spec = importlib.util.spec_from_file_location("user_identity_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "564d0bd33a5c"
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_user_identity_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text('CREATE TABLE "user" (id serial PRIMARY KEY, orcid text)'))
            connection.execute(text("""INSERT INTO "user" (orcid) VALUES ('0000-0002-1825-0097')"""))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT count(*) FROM user_identity")).scalar() == 0
                assert connection.execute(text('SELECT count(*) FROM "user"')).scalar() == 1
                indexes = {index["name"] for index in inspect(connection).get_indexes("user_identity", schema=schema)}
                assert {"ix_user_identity_user_id", "ux_user_identity_one_orcid"} <= indexes
                migration.downgrade()
                assert "user_identity" not in inspect(connection).get_table_names(schema=schema)
        finally:
            transaction.rollback()
    engine.dispose()
