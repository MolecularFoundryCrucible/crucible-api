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


def test_accounts_without_a_username_get_one_and_the_column_becomes_required():
    from tests.pg_fixture import throwaway_database

    with throwaway_database() as engine:
        db._apply_baseline(engine)
        _alembic(engine, command.upgrade, "9d4a2c7e6f13")
        with engine.begin() as connection:
            for orcid, first, last, username in (
                ("o1", "Ada", "Lovelace", "ada_lovelace"), ("o2", "Ada", "Lovelace", None), ("o3", "Admin", "", None), ("o4", "李", "雷", None),
            ):
                connection.execute(text('INSERT INTO "user" (orcid, first_name, last_name, username) VALUES (:o, :f, :l, :u)'),
                                   {"o": orcid, "f": first, "l": last, "u": username})

        _alembic(engine, command.upgrade, "head")
        with engine.connect() as connection:
            names = dict(connection.execute(text('SELECT orcid, username FROM "user" ORDER BY orcid')).all())
            nullable = connection.execute(text(
                "SELECT is_nullable FROM information_schema.columns WHERE table_name = 'user' AND column_name = 'username'")).scalar()
        assert names == {"o1": "ada_lovelace", "o2": "ada_lovelace_2", "o3": "admin_2", "o4": "li_lei"}
        assert nullable == "NO"

        _alembic(engine, command.downgrade, "9d4a2c7e6f13")
