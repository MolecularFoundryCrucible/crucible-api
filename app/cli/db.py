import argparse
import json
import os
from argparse import Namespace
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_REVISION = "b8f2d6e41c07"
BASELINE_SQL = REPO_ROOT / "migrations" / "schema" / f"baseline_{BASELINE_REVISION}.sql"
UPGRADE_LOCK = "hashtext('crucible db upgrade')"


def guarded_database() -> str:
    """Name of the database that migrations refuse to touch without explicit confirmation."""
    return os.getenv("ALEMBIC_PRODUCTION_DATABASE", "crucible")


def upgrade_database(engine, alembic_ini: str | Path = REPO_ROOT / "alembic.ini", *, production_confirmed: bool = False) -> str:
    """Bring the database to the latest schema: an empty database gets the baseline schema and
    then the migrations after it; a database Alembic already manages gets the pending migrations.
    Concurrent runs wait for each other."""
    with engine.connect() as lock:
        lock.execute(text(f"SELECT pg_advisory_lock({UPGRADE_LOCK})"))
        lock.commit()
        try:
            return _upgrade(engine, alembic_ini, production_confirmed)
        finally:
            lock.execute(text(f"SELECT pg_advisory_unlock({UPGRADE_LOCK})"))
            lock.commit()


def _upgrade(engine, alembic_ini, production_confirmed: bool) -> str:
    with engine.connect() as connection:
        database = connection.execute(text("SELECT current_database()")).scalar()
        tables = set(inspect(connection).get_table_names(schema="public"))
    guarded = guarded_database()
    if guarded and database == guarded and not production_confirmed:
        raise RuntimeError(f"{database!r} is the production database; pass --yes-this-is-production if that is intended")
    if not tables:
        _apply_baseline(engine)
        result = "initialized"
    elif "alembic_version" in tables:
        result = "upgraded"
    else:
        raise RuntimeError("The database has tables but no alembic_version table; refusing to guess its schema")

    config = Config(str(alembic_ini), cmd_opts=Namespace(x=["yes-this-is-production=true"] if production_confirmed else []))
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    return result


def _apply_baseline(engine) -> None:
    connection = engine.raw_connection()
    # The dump changes session settings such as search_path; close this connection instead of
    # returning it to the pool.
    connection.detach()
    try:
        with connection.cursor() as cursor:
            cursor.execute(BASELINE_SQL.read_text())
            cursor.execute("RESET ALL")
            cursor.execute("CREATE TABLE public.alembic_version (version_num varchar(32) NOT NULL, CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))")
            cursor.execute("INSERT INTO public.alembic_version (version_num) VALUES (%s)", (BASELINE_REVISION,))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def sequences_behind(connection) -> list[dict]:
    """Id columns whose sequence would next hand out a value the table already holds. That happens
    when rows are loaded with their own ids (copied from another database) and the sequence is not
    moved past them; the next insert then fails on a duplicate key, which the API reports as a 500."""
    columns = connection.execute(text("""
        SELECT n.nspname, t.relname, a.attname, pg_get_serial_sequence(format('%I.%I', n.nspname, t.relname), a.attname)
        FROM pg_class t JOIN pg_namespace n ON n.oid = t.relnamespace JOIN pg_attribute a ON a.attrelid = t.oid
        WHERE n.nspname = ANY (current_schemas(false)) AND t.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
          AND pg_get_serial_sequence(format('%I.%I', n.nspname, t.relname), a.attname) IS NOT NULL
        ORDER BY 1, 2, 3
    """)).all()
    behind = []
    for schema, table, column, sequence in columns:
        last_value, is_called = connection.execute(text(f"SELECT last_value, is_called FROM {sequence}")).one()
        highest = connection.execute(text(f"SELECT max({_quoted(column)}) FROM {_quoted(schema)}.{_quoted(table)}")).scalar()
        next_value = last_value + 1 if is_called else last_value
        if highest is not None and next_value <= highest:
            behind.append({"schema": schema, "table": table, "column": column, "sequence": sequence, "next_value": next_value, "highest_id": highest})
    return behind


def advance_sequences(connection, behind: list[dict]) -> None:
    """Move each sequence to its table's highest id. The value is read again in the same statement
    and compared with where the sequence is by then, so rows or inserts that arrived since the
    report cannot make this move a sequence backwards or leave it short."""
    for entry in behind:
        target = f"{_quoted(entry['schema'])}.{_quoted(entry['table'])}"
        connection.execute(text(
            f"SELECT setval(CAST(:sequence AS regclass), GREATEST((SELECT max({_quoted(entry['column'])}) FROM {target}), (SELECT last_value FROM {entry['sequence']})))"
        ), {"sequence": entry["sequence"]})


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("db", help="Manage the database schema").add_subparsers(dest="command", required=True)
    upgrade = commands.add_parser("upgrade", help="Create the schema in an empty database, or apply pending migrations")
    upgrade.add_argument("--yes-this-is-production", action="store_true", help="Allow running against the production database (ALEMBIC_PRODUCTION_DATABASE)")
    upgrade.set_defaults(handler=_run_upgrade)
    sequences = commands.add_parser("sequences", help="Report id sequences that are behind their tables, which makes inserts fail; run it after loading rows that carry their own ids")
    sequences.add_argument("--commit", action="store_true", help="Move those sequences up to their tables' highest ids; without it nothing is changed")
    sequences.add_argument("--yes-this-is-production", action="store_true", help="Allow --commit against the production database (ALEMBIC_PRODUCTION_DATABASE)")
    sequences.set_defaults(handler=_run_sequences)


def _run_upgrade(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine

    print(upgrade_database(get_engine(), production_confirmed=args.yes_this_is_production))
    return 0


def _run_sequences(args: argparse.Namespace) -> int:
    """Exit 1 from a dry run that finds a sequence behind, so a check can fail on it."""
    from app.api.dependencies import get_engine

    with get_engine().begin() as connection:
        database = connection.execute(text("SELECT current_database()")).scalar()
        behind = sequences_behind(connection)
        if args.commit and behind:
            guarded = guarded_database()
            if guarded and database == guarded and not args.yes_this_is_production:
                raise RuntimeError(f"{database!r} is the production database; pass --yes-this-is-production if that is intended")
            advance_sequences(connection, behind)
    print(json.dumps({"mode": "commit" if args.commit else "dry-run", "database": database, "behind": behind}))
    return 1 if behind and not args.commit else 0
