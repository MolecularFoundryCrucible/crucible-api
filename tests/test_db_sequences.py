import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from app.cli import db as db_cli


@pytest.fixture
def engine(use_engine):
    url = os.getenv("CRUCIBLE_TEST_PG_URL")
    if not url:
        pytest.skip("requires isolated test PostgreSQL")
    admin = create_engine(url)
    schema = "test_sequences_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE keyword (id serial PRIMARY KEY, keyword text NOT NULL)"))
            connection.execute(text("CREATE TABLE note (id serial PRIMARY KEY, body text)"))
            connection.execute(text("INSERT INTO note (body) VALUES ('made here')"))
            connection.execute(text("INSERT INTO keyword (id, keyword) VALUES (1, 'copied'), (2, 'copied too'), (40, 'copied later')"))
        use_engine(engine)
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def run(capsys, **options):
    code = db_cli._run_sequences(SimpleNamespace(**{"commit": False, "yes_this_is_production": False, **options}))
    return code, json.loads(capsys.readouterr().out)


def test_rows_loaded_with_their_own_ids_leave_the_sequence_behind_until_it_is_advanced(engine, capsys):
    with engine.connect() as connection, pytest.raises(Exception, match="duplicate key"):
        connection.execute(text("INSERT INTO keyword (keyword) VALUES ('new')"))
    code, report = run(capsys)
    assert code == 1 and report["mode"] == "dry-run"
    assert [(entry["table"], entry["column"], entry["next_value"], entry["highest_id"]) for entry in report["behind"]] == [("keyword", "id", 2, 40)]
    code, report = run(capsys, commit=True)
    assert code == 0 and [entry["table"] for entry in report["behind"]] == ["keyword"]
    assert run(capsys) == (0, {"mode": "dry-run", "database": report["database"], "behind": []})
    with engine.begin() as connection:
        assert connection.execute(text("INSERT INTO keyword (keyword) VALUES ('new') RETURNING id")).scalar() == 41
        assert connection.execute(text("INSERT INTO note (body) VALUES ('second') RETURNING id")).scalar() == 2


def test_the_production_database_needs_its_confirmation_before_a_change(engine, capsys, monkeypatch):
    with engine.connect() as connection:
        monkeypatch.setenv("ALEMBIC_PRODUCTION_DATABASE", connection.execute(text("SELECT current_database()")).scalar())
    assert run(capsys)[0] == 1
    with pytest.raises(RuntimeError, match="production"):
        db_cli._run_sequences(SimpleNamespace(commit=True, yes_this_is_production=False))
    assert db_cli._run_sequences(SimpleNamespace(commit=True, yes_this_is_production=True)) == 0
