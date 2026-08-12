"""
Backfill dataset.instrument_mfid from dataset.instrument_id (the existing
internal int FK to Instrument.id), resolving through Instrument.unique_id.
Same principle as the project_mfid backfill, applied to instruments — see
docs/permission_system_plan.md, "Project identity: mfid vs project_id".

Uses the existing instrument_id (int) FK rather than re-matching
instrument_name, since instrument_id is already an unambiguous resolved
reference — no risk of the case-insensitive free-text matching finding a
different instrument than the one originally recorded.

Idempotent — only touches rows where instrument_id IS NOT NULL and
instrument_mfid IS NULL, safe to re-run.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260811_backfill_instrument_mfid.py
    DB_CONF_FILE=.env python migrations/data/20260811_backfill_instrument_mfid.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Dataset, Instrument

PRODUCTION_DB = "crucible"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="apply; without it everything is rolled back")
    parser.add_argument("--yes-this-is-production", action="store_true",
                        help=f"required to run against the {PRODUCTION_DB!r} database")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {actual!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (rolled back)'}")

        if actual == PRODUCTION_DB and not args.yes_this_is_production:
            print(f"\nREFUSING TO RUN: {actual!r} is production.")
            print("Re-run with --yes-this-is-production if that is genuinely intended.")
            return 2

        id_to_mfid = dict(session.exec(select(Instrument.id, Instrument.unique_id)).all())

        rows = session.exec(
            select(Dataset).where(Dataset.instrument_id != None, Dataset.instrument_mfid == None)  # noqa: E711
        ).all()
        total = len(rows)
        print(f"datasets missing instrument_mfid: {total}")

        resolved, dangling = 0, 0
        for i, ds in enumerate(rows, 1):
            mfid = id_to_mfid.get(ds.instrument_id)
            if mfid is None:
                dangling += 1
                print(f"  WARNING: dataset {ds.unique_id} has instrument_id {ds.instrument_id!r}, "
                     f"which matches no instrument")
            else:
                ds.instrument_mfid = mfid
                session.add(ds)
                resolved += 1
            if i % 500 == 0 or i == total:
                session.flush()
                print(f"  ... {i}/{total} processed")

        print(f"\n{resolved} resolved, {dangling} dangling (left NULL)")

        if args.commit:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run only: nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
