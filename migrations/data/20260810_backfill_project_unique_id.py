"""
Backfill project.unique_id — a real, independently-generated MFID — for
every existing project.

Purely additive groundwork (see the schema migration, migrations/versions/
20260810_..._add_project_unique_id.py): nothing reads this column yet.
project_id remains the identifier ACL/group lookups and other tables' FKs
use. Idempotent — only touches rows where unique_id IS NULL, safe to re-run.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260810_backfill_project_unique_id.py
    DB_CONF_FILE=.env python migrations/data/20260810_backfill_project_unique_id.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Project, get_mfid_value

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

        projects = session.exec(select(Project).where(Project.unique_id == None)).all()  # noqa: E711
        print(f"projects missing unique_id: {len(projects)}")

        seen = set(session.exec(
            select(Project.unique_id).where(Project.unique_id != None)  # noqa: E711
        ).all())
        total = len(projects)
        for i, proj in enumerate(projects, 1):
            mfid = get_mfid_value()
            while mfid in seen:
                mfid = get_mfid_value()
            seen.add(mfid)
            proj.unique_id = mfid
            session.add(proj)
            # Flush in chunks so each printed line reflects real completed
            # work, not just an in-memory loop racing ahead of the one big
            # flush that would otherwise happen silently at the end.
            if i % 500 == 0 or i == total:
                session.flush()
                print(f"  ... {i}/{total} written")

        nulls_remaining = session.exec(select(Project).where(Project.unique_id == None)).all()  # noqa: E711
        assert not nulls_remaining, f"{len(nulls_remaining)} still null after backfill"

        dupes = session.execute(text(
            "SELECT unique_id, count(*) FROM project WHERE unique_id IS NOT NULL "
            "GROUP BY unique_id HAVING count(*) > 1"
        )).all()
        assert not dupes, f"duplicate unique_id values would result: {dupes}"

        if args.commit:
            session.commit()
            print(f"committed: {len(projects)} projects backfilled")
        else:
            session.rollback()
            print(f"dry run only: {len(projects)} projects would be backfilled, nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
