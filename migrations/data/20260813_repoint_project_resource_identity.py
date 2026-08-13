"""
Repoint every project's own idtype/resourceacl identity from project_id
(the mutable human label) to unique_id (the immutable MFID).

create_project used to seed Resource.mfid and the owner ResourceACL.mfid
with project_id -- the same "identity is a label, not an MFID" bug already
fixed this session for Dataset/Sample/Instrument FKs. See
docs/permission_system_plan.md, "Project's own resource identity". Code
already writing/reading the new identity ships in the same change as this
script; this backfills every project created before that deploy.

resourceacl.mfid FK's to idtype.mfid (idtype's own primary key), so
repointing a project's idtype.mfid to a value nothing yet points at is
blocked by Postgres (NO ACTION) while resourceacl rows still reference the
old value. The FK constraint is dropped and recreated inside the same
transaction -- if anything after the drop fails, the whole transaction rolls
back and the constraint is restored exactly as it was.

scientific_metadata_new and deletionrequest also FK to idtype.mfid, but
(confirmed via direct query before writing this script) hold zero rows for
any project on staging, so no constraint handling is needed for them here.
If that's no longer true when this runs, the script will fail loudly with a
foreign key violation on commit -- that's the point, not a bug: it means
those tables need the same treatment added.

Idempotent -- only touches idtype/resourceacl rows still keyed by a
project's project_id (a project already migrated, where project_id ==
unique_id or nothing matches, is left alone), safe to re-run.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260813_repoint_project_resource_identity.py
    DB_CONF_FILE=.env python migrations/data/20260813_repoint_project_resource_identity.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import Project

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

        projects = session.exec(select(Project)).all()
        to_migrate = [p for p in projects if p.project_id != p.unique_id]
        print(f"projects to repoint: {len(to_migrate)} / {len(projects)}")
        if not to_migrate:
            print("nothing to do")
            return 0

        session.execute(text("ALTER TABLE resourceacl DROP CONSTRAINT resourceacl_mfid_fkey"))

        idtype_updated, acl_updated = 0, 0
        total = len(to_migrate)
        for i, p in enumerate(to_migrate, 1):
            r1 = session.execute(
                text("UPDATE idtype SET mfid = :new WHERE mfid = :old AND resource_type = 'project'"),
                {"new": p.unique_id, "old": p.project_id},
            )
            idtype_updated += r1.rowcount
            r2 = session.execute(
                text("UPDATE resourceacl SET mfid = :new WHERE mfid = :old"),
                {"new": p.unique_id, "old": p.project_id},
            )
            acl_updated += r2.rowcount
            if i % 500 == 0 or i == total:
                print(f"  ... {i}/{total} processed")

        session.execute(text(
            "ALTER TABLE resourceacl ADD CONSTRAINT resourceacl_mfid_fkey "
            "FOREIGN KEY (mfid) REFERENCES idtype (mfid)"
        ))

        print(f"\n{idtype_updated} idtype rows repointed, {acl_updated} resourceacl rows repointed")

        if args.commit:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run only: nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
