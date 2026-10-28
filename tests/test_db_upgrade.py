import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url

from app.cli import db

PG_URL = os.getenv("CRUCIBLE_TEST_PG_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="requires isolated test PostgreSQL")
HEAD = ScriptDirectory(str(db.REPO_ROOT / "migrations")).get_current_head()


@pytest.fixture
def empty_db():
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    name = "crucible_upgrade_" + uuid4().hex[:12]
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(make_url(PG_URL).set(database=name))
    yield engine
    engine.dispose()
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
    admin.dispose()


def _tables(engine):
    with engine.connect() as connection:
        return set(inspect(connection).get_table_names(schema="public"))


@needs_pg
def test_empty_database_is_initialized_to_head(empty_db):
    assert db.upgrade_database(empty_db) == "initialized"
    with empty_db.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [HEAD]
        assert connection.execute(text("SELECT group_type FROM accessgroup WHERE group_name = 'public'")).scalar() == "system"
        extensions = set(connection.execute(text("SELECT extname FROM pg_extension")).scalars())
        assert {"vector", "pg_trgm"} <= extensions
        assert connection.execute(text("SHOW search_path")).scalar() == '"$user", public'


@needs_pg
def test_second_run_is_upgrade_noop(empty_db):
    db.upgrade_database(empty_db)
    assert db.upgrade_database(empty_db) == "upgraded"
    with empty_db.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [HEAD]


@needs_pg
def test_tables_without_revision_refused(empty_db):
    with empty_db.begin() as connection:
        connection.execute(text("CREATE TABLE stray (id integer)"))
    with pytest.raises(RuntimeError, match="alembic_version"):
        db.upgrade_database(empty_db)
    assert _tables(empty_db) == {"stray"}


@needs_pg
def test_failure_midway_leaves_database_empty(empty_db, monkeypatch):
    monkeypatch.setattr(db, "BASELINE_REVISION", "x" * 40)
    with pytest.raises(Exception):
        db.upgrade_database(empty_db)
    assert _tables(empty_db) == set()


@needs_pg
def test_runs_against_given_engine(empty_db, monkeypatch):
    monkeypatch.setenv("DB_SERVER", "nowhere.invalid")
    monkeypatch.setenv("PGDB_PW", "wrong")
    assert db.upgrade_database(empty_db) == "initialized"


@needs_pg
def test_production_guard_refuses_before_changing_anything(empty_db, monkeypatch):
    monkeypatch.setenv("ALEMBIC_PRODUCTION_DATABASE", empty_db.url.database)
    with pytest.raises(RuntimeError, match="production"):
        db.upgrade_database(empty_db)
    assert _tables(empty_db) == set()
    assert db.upgrade_database(empty_db, production_confirmed=True) == "initialized"
    assert db.upgrade_database(empty_db, production_confirmed=True) == "upgraded"


@needs_pg
def test_empty_guard_name_turns_the_guard_off(empty_db, monkeypatch):
    monkeypatch.setenv("ALEMBIC_PRODUCTION_DATABASE", "")
    assert db.upgrade_database(empty_db) == "initialized"


@needs_pg
def test_concurrent_runs_wait_for_each_other(empty_db):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(lambda _: db.upgrade_database(empty_db), range(2)))
    assert results == ["initialized", "upgraded"]


@needs_pg
def test_application_user_with_extensions_created_by_an_administrator(empty_db):
    role = "crucible_app_" + uuid4().hex[:8]
    admin = create_engine(PG_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as connection:
        connection.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD 'app'"))
        connection.execute(text(f'ALTER DATABASE "{empty_db.url.database}" OWNER TO {role}'))
    with empty_db.begin() as connection:
        connection.execute(text("CREATE EXTENSION vector"))
        connection.execute(text("CREATE EXTENSION pg_trgm"))
    app_engine = create_engine(empty_db.url.set(username=role, password="app"))
    try:
        assert db.upgrade_database(app_engine) == "initialized"
    finally:
        app_engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'ALTER DATABASE "{empty_db.url.database}" OWNER TO postgres'))
        with empty_db.begin() as connection:
            connection.execute(text(f"DROP OWNED BY {role}"))
        empty_db.dispose()
        with admin.connect() as connection:
            connection.execute(text(f"DROP ROLE {role}"))
        admin.dispose()


@needs_pg
def test_command_line_upgrades_the_configured_database(empty_db, use_engine, capsys):
    use_engine(empty_db)
    from app.cli.__main__ import main
    assert main(["db", "upgrade"]) == 0
    assert capsys.readouterr().out.strip() == "initialized"


def test_readiness_expects_migration_heads_when_unset(monkeypatch):
    from app.api.routes import misc

    monkeypatch.delenv("EXPECTED_DB_REVISIONS", raising=False)
    assert misc._expected_schema_revisions() == [HEAD]
    assert misc._schema_state([], [HEAD]) == "behind"
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", "b8f2d6e41c07")
    assert misc._expected_schema_revisions() == ["b8f2d6e41c07"]
