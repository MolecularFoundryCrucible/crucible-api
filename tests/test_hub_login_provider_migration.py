import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text

COLUMNS = {"provider_key", "provider_code_verifier", "required_user"}


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_hub_login_provider_migration_roundtrip():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261104_a3d7e1c94b28_hub_login_provider.py"
    spec = importlib.util.spec_from_file_location("hub_login_provider_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_hub_login_provider_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE hublogintransaction (state_hash text PRIMARY KEY)"))
            connection.execute(text("INSERT INTO hublogintransaction (state_hash) VALUES ('in-flight')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT provider_key, provider_code_verifier, required_user FROM hublogintransaction")).one() == (None, None, None)
                migration.downgrade()
                assert not COLUMNS & {c["name"] for c in inspect(connection).get_columns("hublogintransaction", schema=schema)}
        finally:
            transaction.rollback()
    engine.dispose()
