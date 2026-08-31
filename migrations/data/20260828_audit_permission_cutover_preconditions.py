"""Audit the database before final permission enforcement.

This script is read-only and works before or after resourceacl.role is renamed to permission. It exits with status 1 if null roles, duplicate owner ACLs, or incomplete access-group classification would block the final Alembic migration.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260828_audit_permission_cutover_preconditions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


def main() -> int:
    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        columns = session.execute(text("""
            SELECT table_name, column_name
            FROM information_schema.columns
            WHERE (table_name = 'resourceacl' AND column_name IN ('role', 'permission'))
               OR (table_name = 'accessgroup' AND column_name = 'group_type')
        """)).all()
        column_names = {(row.table_name, row.column_name) for row in columns}
        acl_columns = [
            column_name
            for table_name, column_name in column_names
            if table_name == "resourceacl"
        ]

        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")

        if len(acl_columns) != 1:
            print(f"BLOCKED: expected one resourceacl role column, found {sorted(acl_columns)!r}")
            return 1
        if ("accessgroup", "group_type") not in column_names:
            print("BLOCKED: accessgroup.group_type is not installed")
            return 1

        acl_column = acl_columns[0]
        counts = session.execute(text(f"""
            SELECT
                (SELECT count(*) FROM resourceacl WHERE {acl_column} IS NULL) AS null_acl_roles,
                (SELECT count(*) FROM useraccessgrouplink WHERE role IS NULL) AS null_membership_roles,
                (
                    SELECT count(*)
                    FROM (
                        SELECT mfid
                        FROM resourceacl
                        WHERE {acl_column} = 'owner'
                        GROUP BY mfid
                        HAVING count(*) > 1
                    ) duplicate_resources
                ) AS duplicate_owner_acls,
                (SELECT count(*) FROM accessgroup WHERE group_type IS NULL) AS unclassified_groups
        """)).one()

        print(f"ACL column             : {acl_column}")
        print(f"null ACL roles          : {counts.null_acl_roles}")
        print(f"null membership roles   : {counts.null_membership_roles}")
        print(f"duplicate owner ACLs    : {counts.duplicate_owner_acls}")
        print(f"unclassified groups     : {counts.unclassified_groups}")

        blockers = sum(counts)
        if blockers:
            if counts.duplicate_owner_acls:
                duplicate_owners = session.execute(text(f"""
                    SELECT
                        acl.mfid,
                        resource.resource_type,
                        json_agg(
                            json_build_object(
                                'group_id', access_group.id,
                                'group_name', access_group.group_name,
                                'group_type', access_group.group_type::text
                            ) ORDER BY access_group.group_name
                        ) AS owner_groups
                    FROM resourceacl acl
                    JOIN accessgroup access_group ON access_group.id = acl.accessgroup_id
                    JOIN idtype resource ON resource.mfid = acl.mfid
                    WHERE acl.{acl_column} = 'owner'
                    GROUP BY acl.mfid, resource.resource_type
                    HAVING count(*) > 1
                    ORDER BY acl.mfid
                    LIMIT 50
                """)).all()
                print("duplicate owner ACL details:")
                for row in duplicate_owners:
                    print(f"  {row.mfid!r} ({row.resource_type}): {row.owner_groups!r}")
                if counts.duplicate_owner_acls > len(duplicate_owners):
                    print(f"  showing {len(duplicate_owners)} of {counts.duplicate_owner_acls}")
            print(f"BLOCKED: {blockers} permission cutover preconditions failed")
            return 1
        print("PASS: permission cutover preconditions are satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
