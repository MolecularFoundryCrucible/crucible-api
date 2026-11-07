import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_session_grant_migration_roundtrip():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261108_1226f8497119_session_grant_and_device_address.py"
    spec = importlib.util.spec_from_file_location("session_grant_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_session_grant_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE hubtokenfamily (id text PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE device_authorization (device_code_hash text PRIMARY KEY)"))
            connection.execute(text("INSERT INTO hubtokenfamily (id) VALUES ('existing')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT grant_type FROM hubtokenfamily")).scalar() is None
                connection.execute(text("INSERT INTO device_authorization (device_code_hash, requested_from) VALUES ('d', '2001:db8::1')"))
                migration.downgrade()
                assert "grant_type" not in {c["name"] for c in inspect(connection).get_columns("hubtokenfamily", schema=schema)}
                assert "requested_from" not in {c["name"] for c in inspect(connection).get_columns("device_authorization", schema=schema)}
        finally:
            transaction.rollback()
    engine.dispose()
