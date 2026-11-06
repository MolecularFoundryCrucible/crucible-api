"""Alembic environment. Connects through the API's own DB config (get_engine(),
DB_CONF_FILE) instead of alembic.ini's sqlalchemy.url, so there is one place
that knows how to reach a database — the same one every migrations/data
script and the app itself use.

Prod and staging share a Cloud SQL instance, differing only by database name
(see migrations/data/apply_sql.py). The same guard applies here: refuse to
run against production unless explicitly told to. ALEMBIC_PRODUCTION_DATABASE
names the guarded database ("crucible" by default); an empty value turns the
guard off for installations without a separate production database.

`python -m app.cli db upgrade` passes its own connection in
config.attributes["connection"]; otherwise get_engine() is used.

Each migration commits on its own, and waits at most ALEMBIC_LOCK_TIMEOUT (5s by default, 0 to
wait forever) for a table lock, so a migration queued behind a long transaction from a running
pod fails instead of blocking every query on that table behind it. A failed run keeps the
migrations before it; run the upgrade again.

Usage:
    DB_CONF_FILE=.env alembic revision --autogenerate -m "add a column"
    DB_CONF_FILE=.env alembic upgrade head
    DB_CONF_FILE=.env alembic -x yes-this-is-production=true upgrade head
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import text
from sqlmodel import SQLModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.dependencies import get_engine
from app.cli.db import guarded_database
# The only module in app/models declaring table=True models. Importing it is
# what populates SQLModel.metadata for autogenerate to diff against.
import app.models.core_models  # noqa: F401

from alembic import context

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = SQLModel.metadata


def _refuse_if_unguarded_production(connection):
    actual = connection.execute(text("SELECT current_database()")).scalar()
    connection.commit()
    guarded = guarded_database()
    if not guarded or actual != guarded:
        return
    if context.get_x_argument(as_dictionary=True).get("yes-this-is-production") == "true":
        return
    print(f"\nREFUSING TO RUN: {actual!r} is production.")
    print('Re-run with -x yes-this-is-production=true if that is genuinely intended.')
    sys.exit(2)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection — used for --sql review."""
    context.configure(
        url=str(get_engine().url),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        transaction_per_migration=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _limit_lock_waits(connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    connection.execute(text("SELECT set_config('lock_timeout', :value, false)"), {"value": os.getenv("ALEMBIC_LOCK_TIMEOUT") or "5s"})
    connection.commit()


def _reset_lock_waits(connection) -> None:
    if connection.dialect.name != "postgresql":
        return
    connection.rollback()
    connection.execute(text("RESET lock_timeout"))
    connection.commit()


def _run_migrations(connection) -> None:
    _refuse_if_unguarded_production(connection)
    _limit_lock_waits(connection)
    try:
        context.configure(connection=connection, target_metadata=target_metadata, transaction_per_migration=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        _reset_lock_waits(connection)


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_migrations(connection)
        return
    with get_engine().connect() as connection:
        _run_migrations(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
