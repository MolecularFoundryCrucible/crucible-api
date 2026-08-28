"""Generate a 3-to-25-character slug for every instrument missing instrument_id.

The dry run is read-only. The commit sends all assignments in one set-based update and verifies null and duplicate counts before committing.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260810_backfill_instrument_id.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260810_backfill_instrument_id.py --commit
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"
MAX_SLUG_LENGTH = 25
NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(instrument_name: str, fallback: str) -> str:
    slug = NON_SLUG.sub("-", instrument_name.lower()).strip("-")[:MAX_SLUG_LENGTH]
    return slug if len(slug) >= 3 else fallback[:MAX_SLUG_LENGTH]


def deconflict(base: str, seen: set[str]) -> str:
    if base not in seen:
        return base
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = f"{base[:MAX_SLUG_LENGTH - len(suffix)]}{suffix}"
        if candidate not in seen:
            return candidate
        counter += 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="apply changes; otherwise audit only")
    parser.add_argument(
        "--yes-this-is-production",
        action="store_true",
        help=f"required to write to the {PRODUCTION_DB!r} database",
    )
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (read only)'}")
        if database == PRODUCTION_DB and args.commit and not args.yes_this_is_production:
            print(f"REFUSING TO RUN: {database!r} is production.")
            return 2

        instruments = session.execute(text("""
            SELECT id, unique_id, instrument_name
            FROM instrument
            WHERE instrument_id IS NULL
            ORDER BY id
        """)).all()
        seen = set(session.execute(text("""
            SELECT instrument_id FROM instrument WHERE instrument_id IS NOT NULL
        """)).scalars().all())
        assignments = []
        for instrument in instruments:
            base = slugify(instrument.instrument_name, instrument.unique_id)
            instrument_id = deconflict(base, seen)
            seen.add(instrument_id)
            assignments.append({"id": instrument.id, "instrument_id": instrument_id})
            print(f"  {instrument.instrument_name!r} -> {instrument_id!r}")

        print(f"instruments   : {len(assignments)}")
        if not args.commit:
            print("dry run only: nothing written")
            return 0
        if not assignments:
            print("nothing to do")
            return 0

        updated = session.execute(text("""
            UPDATE instrument target
            SET instrument_id = assignment.instrument_id
            FROM jsonb_to_recordset(CAST(:assignments AS jsonb)) AS assignment(id integer, instrument_id text)
            WHERE target.id = assignment.id
              AND target.instrument_id IS NULL
        """), {"assignments": json.dumps(assignments)}).rowcount
        invalid = session.execute(text("""
            SELECT
                count(*) FILTER (WHERE instrument_id IS NULL) AS nulls,
                count(instrument_id) - count(DISTINCT instrument_id) AS duplicates
            FROM instrument
        """)).one()
        if updated != len(assignments) or invalid.nulls or invalid.duplicates:
            session.rollback()
            print(
                "REFUSING TO COMMIT: "
                f"updated={updated}, expected={len(assignments)}, nulls={invalid.nulls}, duplicates={invalid.duplicates}."
            )
            return 2
        session.commit()
        print(f"committed: {updated} instruments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
