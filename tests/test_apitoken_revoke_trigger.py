import hashlib
import importlib.util
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_deleting_a_plain_key_revokes_its_hashed_copy_only():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261106_2b0ab3a4520a_apitoken_revoke_trigger.py"
    spec = importlib.util.spec_from_file_location("apitoken_revoke_trigger", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_apitoken_revoke_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            run = lambda sql, **params: connection.execute(text(sql), params)  # noqa: E731
            run(f'CREATE SCHEMA "{schema}"')
            run(f'SET LOCAL search_path TO "{schema}"')
            run("CREATE TABLE apitoken (token text PRIMARY KEY)")
            run("CREATE TABLE api_key (key_hash text PRIMARY KEY, revoked_at timestamptz)")
            for token in ("old-pod-revokes", "named-key", "unicode-é-key"):
                run("INSERT INTO api_key VALUES (:hash, NULL)", hash=digest(token))
            run("INSERT INTO api_key VALUES (:hash, '2026-01-01T00:00:00Z')", hash=digest("revoked-before"))
            run("INSERT INTO apitoken VALUES ('old-pod-revokes'), ('unicode-é-key'), ('revoked-before'), ('no-copy')")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                run("DELETE FROM apitoken")
                revoked = dict(connection.execute(text("SELECT key_hash, revoked_at FROM api_key")).all())
                assert revoked[digest("old-pod-revokes")] is not None and revoked[digest("unicode-é-key")] is not None
                assert revoked[digest("named-key")] is None
                assert revoked[digest("revoked-before")].isoformat().startswith("2026-01-01")
                migration.downgrade()
                assert connection.execute(text("SELECT count(*) FROM pg_proc WHERE proname = 'revoke_hashed_api_key'")).scalar() == 0
        finally:
            transaction.rollback()
    engine.dispose()
