import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_hub_auth_time_migration_roundtrip():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261012_d94e1f2a7b05_hub_auth_time.py"
    spec = importlib.util.spec_from_file_location("hub_auth_time_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_hub_auth_time_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE hublogincode (code_hash text PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE hubtokenfamily (id text PRIMARY KEY)"))
            connection.execute(text("INSERT INTO hubtokenfamily (id) VALUES ('existing')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT auth_time FROM hubtokenfamily")).scalar() is None
                connection.execute(text("INSERT INTO hublogincode (code_hash, auth_time) VALUES ('code', now())"))
                migration.downgrade()
                for table in ("hublogincode", "hubtokenfamily"):
                    assert "auth_time" not in {c["name"] for c in inspect(connection).get_columns(table, schema=schema)}
        finally:
            transaction.rollback()
    engine.dispose()
