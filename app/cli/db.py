import argparse
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m app.cli.db", description="Manage the Crucible database schema.")
    commands = parser.add_subparsers(dest="command", required=True)
    upgrade = commands.add_parser("upgrade", help="Create the schema in an empty database, or apply pending migrations")
    upgrade.add_argument("--yes-this-is-production", action="store_true", help="Allow running against the production database (ALEMBIC_PRODUCTION_DATABASE)")
    args = parser.parse_args(argv)

    from app.api.dependencies import get_engine

    print(upgrade_database(get_engine(), production_confirmed=args.yes_this_is_production))


if __name__ == "__main__":
    main()
