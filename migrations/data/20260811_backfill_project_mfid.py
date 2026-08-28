"""Backfill dataset.project_mfid and sample.project_mfid from project_id.

The dry run is read-only. The commit uses one set-based update per resource table and leaves labels that match no project unchanged.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260811_backfill_project_mfid.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260811_backfill_project_mfid.py --commit
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

        counts = session.execute(text("""
            SELECT
                (SELECT count(*) FROM dataset target JOIN project p ON p.project_id = target.project_id WHERE target.project_id IS NOT NULL AND target.project_mfid IS NULL) AS dataset_resolved,
                (SELECT count(*) FROM dataset target LEFT JOIN project p ON p.project_id = target.project_id WHERE target.project_id IS NOT NULL AND target.project_mfid IS NULL AND p.id IS NULL) AS dataset_dangling,
                (SELECT count(*) FROM sample target JOIN project p ON p.project_id = target.project_id WHERE target.project_id IS NOT NULL AND target.project_mfid IS NULL) AS sample_resolved,
                (SELECT count(*) FROM sample target LEFT JOIN project p ON p.project_id = target.project_id WHERE target.project_id IS NOT NULL AND target.project_mfid IS NULL AND p.id IS NULL) AS sample_dangling
        """)).one()
        print(f"datasets      : {counts.dataset_resolved} resolvable, {counts.dataset_dangling} dangling")
        print(f"samples       : {counts.sample_resolved} resolvable, {counts.sample_dangling} dangling")
        if not args.commit:
            print("dry run only: nothing written")
            return 0

        dataset_updated = session.execute(text("""
            UPDATE dataset target
            SET project_mfid = p.unique_id
            FROM project p
            WHERE target.project_id = p.project_id
              AND target.project_mfid IS NULL
        """)).rowcount
        sample_updated = session.execute(text("""
            UPDATE sample target
            SET project_mfid = p.unique_id
            FROM project p
            WHERE target.project_id = p.project_id
              AND target.project_mfid IS NULL
        """)).rowcount
        actual = (dataset_updated, sample_updated)
        expected = (counts.dataset_resolved, counts.sample_resolved)
        if actual != expected:
            session.rollback()
            print(f"REFUSING TO COMMIT: expected row counts {expected!r}, got {actual!r}.")
            return 2
        session.commit()
        print(f"committed: {dataset_updated} datasets, {sample_updated} samples")
    return 0


if __name__ == "__main__":
    sys.exit(main())
