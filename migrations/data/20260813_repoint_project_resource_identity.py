"""Move project resource identities from project_id to Project.unique_id.

The script copies each old project idtype row to its MFID, repoints every known foreign-key reference, and removes the old row. It refuses resource collisions, partial duplicate targets, and unexpected foreign-key consumers.

The dry run is read-only. The commit uses a fixed number of set-based statements and never drops a foreign key.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260813_repoint_project_resource_identity.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260813_repoint_project_resource_identity.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"
EXPECTED_REFERENCES = {
    ("accessgroup", "resource_mfid"),
    ("resourceacl", "mfid"),
    ("scientific_metadata_new", "unique_id"),
    ("deletionrequest", "resource_id"),
}


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
            print("Re-run with --yes-this-is-production if that is intended.")
            return 2

        references = set(session.execute(text("""
            SELECT source.relname, source_column.attname
            FROM pg_constraint constraint_row
            JOIN pg_class target ON target.oid = constraint_row.confrelid
            JOIN pg_class source ON source.oid = constraint_row.conrelid
            JOIN pg_attribute source_column
              ON source_column.attrelid = source.oid
             AND source_column.attnum = constraint_row.conkey[1]
            WHERE constraint_row.contype = 'f'
              AND target.relname = 'idtype'
        """)).all())
        unexpected = references - EXPECTED_REFERENCES
        if unexpected:
            print(f"REFUSING TO RUN: unexpected idtype foreign keys: {sorted(unexpected)!r}")
            return 2
        access_group_columns = set(session.execute(text("""
            SELECT column_name FROM information_schema.columns WHERE table_name = 'accessgroup'
        """)).scalars().all())
        has_resource_mfid = "resource_mfid" in access_group_columns

        invalid_old = session.execute(text("""
            SELECT p.project_id, old_resource.resource_type
            FROM project p
            JOIN idtype old_resource ON old_resource.mfid = p.project_id
            WHERE p.project_id <> p.unique_id
              AND old_resource.resource_type <> 'project'
            ORDER BY p.project_id
        """)).all()
        if invalid_old:
            print(f"REFUSING TO RUN: {len(invalid_old)} project IDs name non-project resources.")
            for project_id, resource_type in invalid_old[:50]:
                print(f"  {project_id!r}: {resource_type}")
            return 2

        duplicate_targets = session.execute(text("""
            SELECT p.project_id, p.unique_id, target.resource_type
            FROM project p
            JOIN idtype old_resource
              ON old_resource.mfid = p.project_id
             AND old_resource.resource_type = 'project'
            JOIN idtype target ON target.mfid = p.unique_id
            WHERE p.project_id <> p.unique_id
            ORDER BY p.project_id
        """)).all()
        if duplicate_targets:
            print(f"REFUSING TO RUN: {len(duplicate_targets)} projects have both old and target resources.")
            for project_id, unique_id, resource_type in duplicate_targets[:50]:
                print(f"  {project_id!r} -> {unique_id!r}: target type={resource_type!r}")
            return 2

        access_group_count = (
            "(SELECT count(*) FROM accessgroup ag JOIN targets ON ag.resource_mfid = targets.project_id)"
            if has_resource_mfid
            else "0"
        )
        counts = session.execute(text(f"""
            WITH targets AS (
                SELECT p.project_id, p.unique_id
                FROM project p
                JOIN idtype resource
                  ON resource.mfid = p.project_id
                 AND resource.resource_type = 'project'
                WHERE p.project_id <> p.unique_id
            )
            SELECT
                (SELECT count(*) FROM targets) AS resources,
                (SELECT count(*) FROM resourceacl acl JOIN targets ON acl.mfid = targets.project_id) AS acls,
                {access_group_count} AS access_groups,
                (SELECT count(*) FROM scientific_metadata_new metadata JOIN targets ON metadata.unique_id = targets.project_id) AS metadata,
                (SELECT count(*) FROM deletionrequest request JOIN targets ON request.resource_id = targets.project_id) AS requests
        """)).one()
        print(f"resources     : {counts.resources}")
        print(f"ACL rows      : {counts.acls}")
        print(f"access groups : {counts.access_groups}")
        print(f"metadata rows : {counts.metadata}")
        print(f"requests      : {counts.requests}")
        if not args.commit or counts.resources == 0:
            print("dry run only: nothing written" if not args.commit else "nothing to do")
            return 0

        inserted = session.execute(text("""
            INSERT INTO idtype (
                mfid,
                resource_type,
                creation_time,
                modification_time,
                public,
                created_with_api_version,
                api_version_githash
            )
            SELECT
                p.unique_id,
                resource.resource_type,
                resource.creation_time,
                resource.modification_time,
                resource.public,
                resource.created_with_api_version,
                resource.api_version_githash
            FROM project p
            JOIN idtype resource
              ON resource.mfid = p.project_id
             AND resource.resource_type = 'project'
            WHERE p.project_id <> p.unique_id
        """)).rowcount
        acl_updated = session.execute(text("""
            UPDATE resourceacl acl
            SET mfid = p.unique_id
            FROM project p
            WHERE acl.mfid = p.project_id
              AND p.project_id <> p.unique_id
        """)).rowcount
        groups_updated = 0
        if has_resource_mfid:
            groups_updated = session.execute(text("""
                UPDATE accessgroup target
                SET resource_mfid = p.unique_id
                FROM project p
                WHERE target.resource_mfid = p.project_id
                  AND p.project_id <> p.unique_id
            """)).rowcount
        metadata_updated = session.execute(text("""
            UPDATE scientific_metadata_new metadata
            SET unique_id = p.unique_id
            FROM project p
            WHERE metadata.unique_id = p.project_id
              AND p.project_id <> p.unique_id
        """)).rowcount
        requests_updated = session.execute(text("""
            UPDATE deletionrequest request
            SET resource_id = p.unique_id
            FROM project p
            WHERE request.resource_id = p.project_id
              AND p.project_id <> p.unique_id
        """)).rowcount
        deleted = session.execute(text("""
            DELETE FROM idtype resource
            USING project p
            WHERE resource.mfid = p.project_id
              AND resource.resource_type = 'project'
              AND p.project_id <> p.unique_id
        """)).rowcount

        actual = (inserted, acl_updated, groups_updated, metadata_updated, requests_updated, deleted)
        expected = (
            counts.resources,
            counts.acls,
            counts.access_groups,
            counts.metadata,
            counts.requests,
            counts.resources,
        )
        if actual != expected:
            session.rollback()
            print(f"REFUSING TO COMMIT: expected row counts {expected!r}, got {actual!r}.")
            return 2
        session.commit()
        print(f"committed: {inserted} project resources repointed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
