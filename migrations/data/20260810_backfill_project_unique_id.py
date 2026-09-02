"""Generate an MFID for every project missing Project.unique_id.

The dry run is read-only. The commit sends all generated identifiers in one set-based update and verifies null and duplicate counts before committing.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260810_backfill_project_unique_id.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260810_backfill_project_unique_id.py --commit
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.models.core_models import get_mfid_value


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

        project_ids = session.execute(text("SELECT id FROM project WHERE unique_id IS NULL ORDER BY id")).scalars().all()
        seen = set(session.execute(text("SELECT unique_id FROM project WHERE unique_id IS NOT NULL")).scalars().all())
        assignments = []
        for project_id in project_ids:
            unique_id = get_mfid_value()
            while unique_id in seen:
                unique_id = get_mfid_value()
            seen.add(unique_id)
            assignments.append({"id": project_id, "unique_id": unique_id})

        print(f"projects      : {len(assignments)}")
        if not args.commit:
            print("dry run only: nothing written")
            return 0
        if not assignments:
            print("nothing to do")
            return 0

        updated = session.execute(text("""
            UPDATE project target
            SET unique_id = assignment.unique_id
            FROM jsonb_to_recordset(CAST(:assignments AS jsonb)) AS assignment(id integer, unique_id text)
            WHERE target.id = assignment.id
              AND target.unique_id IS NULL
        """), {"assignments": json.dumps(assignments)}).rowcount
        invalid = session.execute(text("""
            SELECT
                count(*) FILTER (WHERE unique_id IS NULL) AS nulls,
                count(*) - count(DISTINCT unique_id) AS duplicates
            FROM project
        """)).one()
        if updated != len(assignments) or invalid.nulls or invalid.duplicates:
            session.rollback()
            print(
                "REFUSING TO COMMIT: "
                f"updated={updated}, expected={len(assignments)}, nulls={invalid.nulls}, duplicates={invalid.duplicates}."
            )
            return 2
        session.commit()
        print(f"committed: {updated} projects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
