import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_multipart_migration_roundtrip():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20260928_b72f6c903a18_multipart_foundation.py"
    spec = importlib.util.spec_from_file_location("multipart_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_multipart_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text("CREATE TABLE upload_sessions (id serial PRIMARY KEY, owner_user_id text, dataset_mfid text, expires_at timestamptz)"))
            connection.execute(text("CREATE TABLE associatedfile (id serial PRIMARY KEY, sha256_hash text)"))
            connection.execute(text("INSERT INTO upload_sessions (owner_user_id, dataset_mfid, expires_at) VALUES ('owner', 'ds', now())"))
            connection.execute(text("INSERT INTO associatedfile (sha256_hash) VALUES ('legacy-unverified')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert connection.execute(text("SELECT protocol FROM upload_sessions")).scalar() is None
                assert connection.execute(text("SELECT sha256_verified_at FROM associatedfile")).scalar() is None
                connection.execute(text("INSERT INTO upload_sessions (owner_user_id, dataset_mfid) VALUES ('owner', 'ds')"))
                insert = text("INSERT INTO upload_sessions (owner_user_id, dataset_mfid, protocol, state, idempotency_key, part_manifest) VALUES ('owner', 'ds', 'gcs_xml_v1', 'initiating', 'key', '{}')")
                connection.execute(insert)
                with pytest.raises(IntegrityError):
                    with connection.begin_nested():
                        connection.execute(insert)
                with pytest.raises(IntegrityError):
                    with connection.begin_nested():
                        connection.execute(text("INSERT INTO upload_sessions (protocol) VALUES ('gcs_xml_v1')"))
                migration.downgrade()
                assert {c["name"] for c in inspect(connection).get_columns("upload_sessions", schema=schema)} == {"id", "owner_user_id", "dataset_mfid", "expires_at"}
                assert connection.execute(text("SELECT sha256_hash FROM associatedfile")).scalar() == "legacy-unverified"
                migration.upgrade()
        finally:
            transaction.rollback()
    engine.dispose()
