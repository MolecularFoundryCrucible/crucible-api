"""Repair the four malformed ACL rows on one legacy staging sample.

The repair assigns owner to the personal group, assigns admin to the project group, and removes the obsolete global admin and read_only grants. It refuses to run against any database other than crucible-staging and validates the complete target state before writing.

Dry run by default.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_repair_staging_null_sample_acl.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_repair_staging_null_sample_acl.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine

STAGING_DB = "crucible-staging"
SAMPLE_MFID = "0tkpgy57k5w9h000swxzek7wrr"
EXPECTED_ROWS = {
    "0000-0001-6402-3752": (True, True, "owner"),
    "0tkvpezyz1zzf00076nahf85j4": (True, True, "admin"),
    "admin": (True, True, None),
    "read_only": (True, False, None),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="apply the repair; without it all changes are rolled back")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {actual!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (rolled back)'}")

        if actual != STAGING_DB:
            print(f"\nREFUSING TO RUN: expected {STAGING_DB!r}, got {actual!r}.")
            return 2

        resource_type = session.execute(
            text("SELECT resource_type FROM idtype WHERE mfid = :mfid"),
            {"mfid": SAMPLE_MFID},
        ).scalar_one_or_none()
        if resource_type != "sample":
            print(f"\nREFUSING TO RUN: {SAMPLE_MFID!r} is not a sample.")
            return 2

        rows = session.execute(
            text("""
                SELECT g.group_name, a.read, a.write
                FROM resourceacl a
                JOIN accessgroup g ON g.id = a.accessgroup_id
                WHERE a.mfid = :mfid AND a.permission IS NULL
                ORDER BY g.group_name
            """),
            {"mfid": SAMPLE_MFID},
        ).all()
        actual_rows = {name: (read, write) for name, read, write in rows}
        expected_rows = {name: values[:2] for name, values in EXPECTED_ROWS.items()}

        total_null = session.execute(text("SELECT count(*) FROM resourceacl WHERE permission IS NULL")).scalar_one()
        if actual_rows != expected_rows or total_null != len(EXPECTED_ROWS):
            print("\nREFUSING TO RUN: staging null ACL state does not match the reviewed target.")
            print(f"target rows   : {actual_rows}")
            print(f"all null rows : {total_null}")
            return 2

        print(f"\nsample        : {SAMPLE_MFID}")
        for group_name, (read, write, permission) in EXPECTED_ROWS.items():
            action = f"set permission={permission!r}" if permission else "delete obsolete grant"
            print(f"  {group_name:<28} read={read!s:<5} write={write!s:<5} {action}")

        for group_name, (_, _, permission) in EXPECTED_ROWS.items():
            if permission is None:
                session.execute(
                    text("""
                        DELETE FROM resourceacl a
                        USING accessgroup g
                        WHERE a.mfid = :mfid
                          AND a.accessgroup_id = g.id
                          AND g.group_name = :group_name
                          AND a.permission IS NULL
                    """),
                    {"mfid": SAMPLE_MFID, "group_name": group_name},
                )
            else:
                session.execute(
                    text("""
                        UPDATE resourceacl a
                        SET permission = CAST(:permission AS resource_role)
                        FROM accessgroup g
                        WHERE a.mfid = :mfid
                          AND a.accessgroup_id = g.id
                          AND g.group_name = :group_name
                          AND a.permission IS NULL
                    """),
                    {"mfid": SAMPLE_MFID, "group_name": group_name, "permission": permission},
                )

        remaining = session.execute(text("SELECT count(*) FROM resourceacl WHERE permission IS NULL")).scalar_one()
        if remaining:
            print(f"\nREFUSING TO COMMIT: {remaining} null ACL rows remain.")
            session.rollback()
            return 2

        if args.commit:
            session.commit()
            print("\ncommitted")
        else:
            session.rollback()
            print("\ndry run only: nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
