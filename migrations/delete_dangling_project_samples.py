"""
Delete samples whose project_id names a project that does not exist.

`Sample.project_id` has no foreign key (docs/schema_hygiene.md item 2), so a
mistyped value is stored happily. The affected samples appear in no project
listing. See ~/crucible-audits/dangling-sample-projects.md for the audit and
the project leads' confirmation that this data is disposable.

Deletion goes through the same code path as the API: a DeletionAuditLog row is
written first, then app.utils.deletion_utils.hard_delete_resource does the
cascade. Hand-written SQL was considered and rejected — the cascade covers
DatasetSampleLink, SampleLink, ScientificMetadataBinary, ResourceACL,
DeletionRequest, Resource and Sample, and any hand-rolled version silently
rots the moment a table is added.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/delete_dangling_project_samples.py
    DB_CONF_FILE=.env python migrations/delete_dangling_project_samples.py --commit
"""
import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import get_utc_isoformat
from app.models.core_models import DeletionAuditLog, Project, Resource, Sample
from app.utils.deletion_utils import hard_delete_resource

PRODUCTION_DB = "crucible"
REASON = "dangling project_id; confirmed disposable by project lead"


def find_dangling(session):
    """Samples whose project_id matches no project. Materialised up front —
    the set is defined by a join that the deletions themselves change."""
    return session.execute(text("""
        SELECT s.unique_id, s.sample_name, s.project_id, s.owner_orcid
        FROM sample s
        LEFT JOIN project p ON p.project_id = s.project_id
        WHERE s.project_id IS NOT NULL AND p.project_id IS NULL
        ORDER BY s.project_id, s.unique_id
    """)).all()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="apply; without it everything is rolled back")
    parser.add_argument("--yes-this-is-production", action="store_true",
                        help=f"required to run against the {PRODUCTION_DB!r} database")
    parser.add_argument("--actor", default="migration:delete_dangling_project_samples",
                        help="recorded as requester/reviewer on the audit rows")
    parser.add_argument("--csv", type=Path, default=None,
                        help="write the deleted set to this path before deleting")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {actual!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (rolled back)'}")

        if actual == PRODUCTION_DB and not args.yes_this_is_production:
            print(f"\nREFUSING TO RUN: {actual!r} is production.")
            print("Re-run with --yes-this-is-production if that is intended.")
            return 2

        rows = find_dangling(session)
        if not rows:
            print("\nnothing to delete")
            return 0

        by_project = {}
        for _, _, project_id, _ in rows:
            by_project[project_id] = by_project.get(project_id, 0) + 1
        print(f"\n{len(rows)} samples to delete:")
        for project_id, n in sorted(by_project.items(), key=lambda kv: -kv[1]):
            print(f"  {project_id!r:<34} {n}")

        if args.csv:
            with open(args.csv, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["sample_mfid", "sample_name", "missing_project_id", "owner_orcid"])
                w.writerows(rows)
            print(f"\nwrote the deleted set to {args.csv}")

        deleted = 0
        for mfid, sample_name, project_id, owner_orcid in rows:
            resource = session.exec(select(Resource).where(Resource.mfid == mfid)).one_or_none()
            if resource is None:
                print(f"  {mfid}: no resource row, skipping")
                continue

            # Audit row first, mirroring hard_delete_resource_route. project_id
            # is recorded verbatim — it is the whole reason the sample is going.
            session.add(DeletionAuditLog(
                resource_id=mfid,
                resource_type=resource.resource_type,
                resource_name=sample_name,
                project_id=project_id,
                requester_id=args.actor,
                reason=REASON,
                request_time=None,
                reviewer_id=args.actor,
                reviewer_notes=f"project_id {project_id!r} matches no project",
                review_time=get_utc_isoformat(),
                deleted_at=get_utc_isoformat(),
            ))
            hard_delete_resource(mfid, session)
            deleted += 1
            if deleted % 100 == 0:
                print(f"  {deleted}/{len(rows)}")

        print(f"\n{deleted} samples deleted, {deleted} audit rows written")

        # Explicit: raw text() execution does not autoflush the way an ORM
        # query does, so the verification below would otherwise read the
        # pre-delete state and roll back every run.
        session.flush()

        remaining = len(find_dangling(session))
        orphan_resources = session.execute(text("""
            SELECT count(*) FROM idtype r WHERE r.resource_type = 'sample'
              AND NOT EXISTS (SELECT 1 FROM sample s WHERE s.unique_id = r.mfid)
        """)).scalar()
        print(f"  dangling samples remaining      {remaining}")
        print(f"  sample resource rows orphaned   {orphan_resources}")

        if remaining or orphan_resources:
            session.rollback()
            print("\nROLLED BACK — verification failed")
            return 1

        if args.commit:
            session.commit()
            print("\nCOMMITTED")
        else:
            session.rollback()
            print("\nrolled back (dry run) — re-run with --commit to apply")

    return 0


if __name__ == "__main__":
    sys.exit(main())
