"""Remove the reviewed orphaned production resource with an empty MFID.

The script requires exactly one idtype row with an empty MFID, resource type sample, and no dependent entity, ACL, metadata, or deletion-request rows. It defaults to a read-only dry run and refuses commit outside the production database.

Usage:
    DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20260831_cleanup_empty_mfid_resource.py --yes-this-is-production
    DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20260831_cleanup_empty_mfid_resource.py --commit --yes-this-is-production
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
    parser.add_argument("--commit", action="store_true", help="delete the reviewed orphan")
    parser.add_argument("--yes-this-is-production", action="store_true")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()"), {}).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (read only)'}")

        if args.commit and (database != PRODUCTION_DB or not args.yes_this_is_production):
            print("REFUSING TO COMMIT: production database and confirmation flag are required")
            return 2

        rows = session.execute(text("""
            SELECT
                resource_type,
                (SELECT count(*) FROM sample WHERE unique_id = '') AS samples,
                (SELECT count(*) FROM dataset WHERE unique_id = '') AS datasets,
                (SELECT count(*) FROM instrument WHERE unique_id = '') AS instruments,
                (SELECT count(*) FROM project WHERE project_id = '') AS projects,
                (SELECT count(*) FROM resourceacl WHERE mfid = '') AS acl_rows,
                (SELECT count(*) FROM scientific_metadata_new WHERE unique_id = '') AS metadata_rows,
                (SELECT count(*) FROM deletionrequest WHERE resource_id = '') AS deletion_requests
            FROM idtype
            WHERE mfid = ''
        """)).all()

        if not rows:
            print("nothing to do: no empty-MFID resource exists")
            return 0
        if len(rows) != 1:
            print(f"REFUSING TO RUN: expected at most one reviewed row, found {len(rows)}")
            return 2

        row = rows[0]
        dependencies = (
            row.samples + row.datasets + row.instruments + row.projects
            + row.acl_rows + row.metadata_rows + row.deletion_requests
        )
        print(
            f"resource_type : {row.resource_type!r}\n"
            f"dependencies  : samples={row.samples}, datasets={row.datasets}, "
            f"instruments={row.instruments}, projects={row.projects}, acl_rows={row.acl_rows}, "
            f"metadata_rows={row.metadata_rows}, deletion_requests={row.deletion_requests}"
        )
        if row.resource_type != "sample" or dependencies != 0:
            print("REFUSING TO RUN: the empty-MFID row is not the reviewed orphan")
            return 2
        if not args.commit:
            print("dry run only: nothing written")
            return 0

        deleted = session.execute(text("""
            DELETE FROM idtype
            WHERE mfid = '' AND resource_type = 'sample'
        """)).rowcount
        if deleted != 1:
            session.rollback()
            print(f"REFUSING TO COMMIT: expected one deletion, got {deleted}")
            return 2
        session.commit()
        print("committed: removed one orphaned empty-MFID sample resource")
    return 0


if __name__ == "__main__":
    sys.exit(main())
