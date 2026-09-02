"""Alembic environment. Connects through the API's own DB config (get_engine(),
DB_CONF_FILE) instead of alembic.ini's sqlalchemy.url, so there is one place
that knows how to reach a database — the same one every migrations/data
script and the app itself use.

Prod and staging share a Cloud SQL instance, differing only by database name
(see migrations/data/apply_sql.py). The same guard applies here: refuse to
run against production unless explicitly told to.

Usage:
    DB_CONF_FILE=.env alembic revision --autogenerate -m "add a column"
    DB_CONF_FILE=.env alembic upgrade head
    DB_CONF_FILE=.env alembic -x yes-this-is-production=true upgrade head
"""
import os
import sys
from logging.config import fileConfig

from sqlalchemy import text
from sqlmodel import Session, SQLModel

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.api.dependencies import get_engine
# The only module in app/models declaring table=True models. Importing it is
# what populates SQLModel.metadata for autogenerate to diff against.
import app.models.core_models  # noqa: F401

from alembic import context

PRODUCTION_DB = "crucible"

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def _refuse_if_unguarded_production(engine):
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
    if actual != PRODUCTION_DB:
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
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = get_engine()
    _refuse_if_unguarded_production(engine)

    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
