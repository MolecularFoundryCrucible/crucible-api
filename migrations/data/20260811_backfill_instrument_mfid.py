"""Backfill dataset.instrument_mfid from registered instruments.

The existing instrument row ID is authoritative. Remaining rows resolve by a unique case-insensitive instrument name. Ambiguous registered names block the operation.

The dry run is read-only. The commit uses two set-based updates.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260811_backfill_instrument_mfid.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260811_backfill_instrument_mfid.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"


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

        ambiguous = session.execute(text("""
            SELECT lower(instrument_name), array_agg(unique_id ORDER BY unique_id)
            FROM instrument
            GROUP BY lower(instrument_name)
            HAVING count(*) > 1
            ORDER BY lower(instrument_name)
        """)).all()
        if ambiguous:
            print(f"REFUSING TO RUN: {len(ambiguous)} case-insensitive instrument-name collisions.")
            for instrument_name, mfids in ambiguous[:50]:
                print(f"  {instrument_name!r}: {list(mfids)!r}")
            return 2

        counts = session.execute(text("""
            SELECT
                count(*) FILTER (WHERE by_id.id IS NOT NULL) AS by_id,
                count(*) FILTER (WHERE by_id.id IS NULL AND by_name.id IS NOT NULL) AS by_name,
                count(*) FILTER (WHERE by_id.id IS NULL AND by_name.id IS NULL) AS dangling
            FROM dataset target
            LEFT JOIN instrument by_id ON by_id.id = target.instrument_id
            LEFT JOIN instrument by_name ON lower(by_name.instrument_name) = lower(target.instrument_name)
            WHERE target.instrument_mfid IS NULL
              AND (target.instrument_id IS NOT NULL OR target.instrument_name IS NOT NULL)
        """)).one()
        print(f"by ID         : {counts.by_id}")
        print(f"by name       : {counts.by_name}")
        print(f"dangling      : {counts.dangling}")
        if not args.commit:
            print("dry run only: nothing written")
            return 0

        by_id_updated = session.execute(text("""
            UPDATE dataset target
            SET instrument_mfid = instrument.unique_id
            FROM instrument
            WHERE target.instrument_id = instrument.id
              AND target.instrument_mfid IS NULL
        """)).rowcount
        by_name_updated = session.execute(text("""
            UPDATE dataset target
            SET instrument_id = instrument.id,
                instrument_mfid = instrument.unique_id
            FROM instrument
            WHERE lower(target.instrument_name) = lower(instrument.instrument_name)
              AND target.instrument_mfid IS NULL
        """)).rowcount
        actual = (by_id_updated, by_name_updated)
        expected = (counts.by_id, counts.by_name)
        if actual != expected:
            session.rollback()
            print(f"REFUSING TO COMMIT: expected row counts {expected!r}, got {actual!r}.")
            return 2
        session.commit()
        print(f"committed: {by_id_updated} by ID, {by_name_updated} by name")
    return 0


if __name__ == "__main__":
    sys.exit(main())
