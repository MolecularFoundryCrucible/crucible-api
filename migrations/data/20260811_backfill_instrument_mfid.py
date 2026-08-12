"""
Backfill dataset.instrument_mfid, resolving through Instrument.unique_id.
Same principle as the project_mfid backfill, applied to instruments, see
docs/permission_system_plan.md, "Project identity: mfid vs project_id".

Two resolution paths, tried in order:
  1. dataset.instrument_id (the existing internal int FK to Instrument.id) -
     already an unambiguous resolved reference, no matching needed.
  2. dataset.instrument_name, matched case-insensitively against a
     registered Instrument.instrument_name - for historical rows created
     before create_dataset did case-insensitive matching at write time.
     instrument_name is always stored lowercased at registration, so a
     dataset recorded as "ALS-BL733" against a registered "als-bl733" never
     resolved instrument_id at creation and has sat unlinked ever since.
     This path backfills instrument_id too, not just instrument_mfid, since
     it was never legitimately something else - just never looked up
     case-insensitively at the time.

A dataset whose instrument_name matches no registered instrument by either
path is left NULL and logged, not treated as an error.

Idempotent - only touches rows where instrument_mfid IS NULL, safe to re-run.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260811_backfill_instrument_mfid.py
    DB_CONF_FILE=.env python migrations/data/20260811_backfill_instrument_mfid.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import or_, text
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

        instruments = session.exec(select(Instrument)).all()
        id_to_mfid = {inst.id: inst.unique_id for inst in instruments}
        name_to_instrument = {inst.instrument_name.lower(): inst for inst in instruments}

        rows = session.exec(
            select(Dataset).where(
                Dataset.instrument_mfid == None,  # noqa: E711
                or_(Dataset.instrument_id != None, Dataset.instrument_name != None),  # noqa: E711
            )
        ).all()
        total = len(rows)
        print(f"datasets missing instrument_mfid: {total}")

        resolved_by_id, resolved_by_name, dangling = 0, 0, 0
        for i, ds in enumerate(rows, 1):
            mfid = id_to_mfid.get(ds.instrument_id) if ds.instrument_id is not None else None
            if mfid is not None:
                ds.instrument_mfid = mfid
                session.add(ds)
                resolved_by_id += 1
            elif ds.instrument_name and ds.instrument_name.lower() in name_to_instrument:
                inst = name_to_instrument[ds.instrument_name.lower()]
                ds.instrument_id = inst.id
                ds.instrument_mfid = inst.unique_id
                session.add(ds)
                resolved_by_name += 1
            else:
                dangling += 1
                print(f"  WARNING: dataset {ds.unique_id} has instrument_name {ds.instrument_name!r} "
                     f"/ instrument_id {ds.instrument_id!r}, which matches no registered instrument")
            if i % 500 == 0 or i == total:
                session.flush()
                print(f"  ... {i}/{total} processed")

        print(f"\n{resolved_by_id} resolved via instrument_id, "
             f"{resolved_by_name} resolved via case-insensitive instrument_name, "
             f"{dangling} dangling (left NULL)")

        if args.commit:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run only: nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
