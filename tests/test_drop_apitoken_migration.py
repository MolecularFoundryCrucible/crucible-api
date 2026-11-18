import hashlib
import os

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.cli import db

pytestmark = pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")


def _alembic(engine, action, revision):
    config = Config(str(db.REPO_ROOT / "alembic.ini"))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        action(config, revision)


def test_plain_keys_are_copied_then_the_table_is_dropped():
    from tests.pg_fixture import throwaway_database

    with throwaway_database() as engine:
        db._apply_baseline(engine)
        _alembic(engine, command.upgrade, "119e02fa5b77")
        with engine.begin() as connection:
            connection.execute(text('INSERT INTO "user" (orcid, first_name, last_name, username) VALUES (\'0000-0001-0000-0001\', \'A\', \'L\', \'ada\')'))
            ada = connection.execute(text('SELECT id FROM "user"')).scalar()
            already = hashlib.sha256(b"c" * 64).hexdigest()
            connection.execute(text("INSERT INTO api_key (user_id, key_hash, key_last4, name, created_at) VALUES (:u, :h, 'cccc', 'Default key', now())"), {"u": ada, "h": already})
            for token, created, owner in (("a" * 64, "2026-06-01", "0000-0001-0000-0001"), ("b" * 64, "not a date", "0000-0001-0000-0001"), ("c" * 64, "2026-06-02", "0000-0001-0000-0001"), ("d" * 64, "2026-06-03", None)):
                connection.execute(text("INSERT INTO apitoken (access_group_id, token, token_created, user_unique_id) VALUES ('g', :t, :c, :o)"), {"t": token, "c": created, "o": owner})

        _alembic(engine, command.upgrade, "b0e044b67c87")
        with engine.connect() as connection:
            copied = connection.execute(text("SELECT key_last4, name, created_at::date::text, (expires_at - created_at)::text, revoked_at FROM api_key ORDER BY key_last4")).all()
            assert [tuple(row) for row in copied] == [("aaaa", "Default key", "2026-06-01", "365 days", None), ("cccc", "Default key", copied[1][2], copied[1][3], None)]
            assert connection.execute(text("SELECT to_regclass('apitoken')")).scalar() is None
            assert connection.execute(text("SELECT count(*) FROM pg_proc WHERE proname = 'revoke_hashed_api_key'")).scalar() == 0

        _alembic(engine, command.downgrade, "119e02fa5b77")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM apitoken")).scalar() == 0
        _alembic(engine, command.upgrade, "head")
