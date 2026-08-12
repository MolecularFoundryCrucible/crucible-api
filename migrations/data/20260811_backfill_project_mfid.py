"""
Backfill dataset.project_mfid / sample.project_mfid from project_id,
resolving through Project.unique_id — see docs/permission_system_plan.md,
"Project identity: mfid vs project_id", and the schema migration
migrations/versions/20260811_..._add_project_mfid_to_dataset_and_sample_.py.

Idempotent — only touches rows where project_id IS NOT NULL and
project_mfid IS NULL, safe to re-run. A project_id that doesn't match any
real project is left NULL and logged, not treated as an error — this is a
known pre-existing data-quality issue for Sample.project_id in particular
(it never had a real FK before this), not something this script should paper
over by inventing a match.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260811_backfill_project_mfid.py
    DB_CONF_FILE=.env python migrations/data/20260811_backfill_project_mfid.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Dataset, Project, Sample

PRODUCTION_DB = "crucible"


def backfill(session, model, label, id_to_mfid):
    rows = session.exec(
        select(model).where(model.project_id != None, model.project_mfid == None)  # noqa: E711
    ).all()
    total = len(rows)
    print(f"{label} missing project_mfid: {total}")

    resolved, dangling = 0, 0
    for i, row in enumerate(rows, 1):
        mfid = id_to_mfid.get(row.project_id)
        if mfid is None:
            dangling += 1
            print(f"  WARNING: {label} {row.unique_id} has project_id {row.project_id!r}, which matches no project")
        else:
            row.project_mfid = mfid
            session.add(row)
            resolved += 1
        if i % 500 == 0 or i == total:
            session.flush()
            print(f"  ... {i}/{total} processed")

    print(f"{label}: {resolved} resolved, {dangling} dangling (left NULL)")
    return resolved, dangling


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

        id_to_mfid = dict(session.exec(select(Project.project_id, Project.unique_id)).all())

        ds_resolved, ds_dangling = backfill(session, Dataset, "dataset", id_to_mfid)
        samp_resolved, samp_dangling = backfill(session, Sample, "sample", id_to_mfid)

        if args.commit:
            session.commit()
            print(f"\ncommitted: {ds_resolved + samp_resolved} rows backfilled, "
                 f"{ds_dangling + samp_dangling} left dangling")
        else:
            session.rollback()
            print(f"\ndry run only: {ds_resolved + samp_resolved} rows would be backfilled, "
                 f"{ds_dangling + samp_dangling} would be left dangling, nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
