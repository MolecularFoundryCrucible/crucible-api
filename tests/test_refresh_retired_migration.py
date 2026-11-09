import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_refresh_retired_migration_roundtrip():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261110_208288a60073_refresh_token_retired_at.py"
    spec = importlib.util.spec_from_file_location("refresh_retired_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_refresh_retired_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE hubrefreshtoken (token_hash text PRIMARY KEY)"))
            connection.execute(text("INSERT INTO hubrefreshtoken (token_hash) VALUES ('existing')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT retired_at FROM hubrefreshtoken")).scalar() is None
                migration.downgrade()
                assert "retired_at" not in {c["name"] for c in inspect(connection).get_columns("hubrefreshtoken", schema=schema)}
        finally:
            transaction.rollback()
    engine.dispose()
