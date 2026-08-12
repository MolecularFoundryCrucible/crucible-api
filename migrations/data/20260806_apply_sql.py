"""
Apply a .sql migration file using the API's own database configuration.

Resolves the connection through get_engine(), so no hunting for the password
that get_secret() already knows. Executes the file as one script rather than
splitting on ';' — several migrations contain DO $$ ... $$ blocks with their
own semicolons.

Prod and staging share a Cloud SQL instance, so the target is read back from
the server and production is refused unless explicitly named.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260806_apply_sql.py migrations/data/20260806_add_permission_roles.sql
    DB_CONF_FILE=.env python migrations/data/20260806_apply_sql.py <file> --yes-this-is-production
"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine

PRODUCTION_DB = "crucible"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sql_file", type=Path)
    parser.add_argument("--yes-this-is-production", action="store_true",
                        help=f"required to run against the {PRODUCTION_DB!r} database")
    args = parser.parse_args()

    if not args.sql_file.is_file():
        print(f"no such file: {args.sql_file}")
        return 1

    engine = get_engine()
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
        user = session.execute(text("SELECT current_user")).scalar()

    print(f"applying {args.sql_file}")
    print(f"connected to  : {actual!r} as {user!r} on {engine.url.host}:{engine.url.port}")

    if actual == PRODUCTION_DB and not args.yes_this_is_production:
        print(f"\nREFUSING TO RUN: {actual!r} is production.")
        print("Re-run with --yes-this-is-production if that is genuinely intended.")
        return 2

    # raw_connection + autocommit so the file's own BEGIN/COMMIT and any
    # DO $$ ... $$ blocks execute as written.
    raw = engine.raw_connection()
    try:
        raw.autocommit = True
        with raw.cursor() as cur:
            cur.execute(args.sql_file.read_text())
    finally:
        raw.close()

    print("applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
