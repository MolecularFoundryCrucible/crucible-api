"""Remove the reviewed legacy unknown group from staging.

The script requires the exact reviewed duplicate-owner state: unknown is a custom group, it owns project 0tkvpezyr5w3b000stgq42knzr, the canonical project group also owns that project, and unknown has no join requests. Commit removes the legacy ACL, memberships, and group.

The script refuses every database except crucible-staging and defaults to a read-only dry run.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260828_remove_staging_unknown_group.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260828_remove_staging_unknown_group.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


STAGING_DB = "crucible-staging"
LEGACY_GROUP = "unknown"
PROJECT_MFID = "0tkvpezyr5w3b000stgq42knzr"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="remove the reviewed group")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (read only)'}")
        if database != STAGING_DB:
            print(f"REFUSING TO RUN: expected {STAGING_DB!r}, got {database!r}.")
            return 2

        state = session.execute(text("""
            SELECT
                legacy.id AS legacy_group_id,
                legacy.group_type::text AS legacy_group_type,
                canonical.id AS canonical_group_id,
                canonical.group_type::text AS canonical_group_type,
                legacy_acl.permission::text AS legacy_permission,
                canonical_acl.permission::text AS canonical_permission,
                (
                    SELECT count(*)
                    FROM accessgroupjoinrequest request
                    WHERE request.accessgroup_id = legacy.id
                ) AS request_count
            FROM accessgroup legacy
            JOIN accessgroup canonical ON canonical.group_name = :project_mfid
            JOIN resourceacl legacy_acl
              ON legacy_acl.accessgroup_id = legacy.id
             AND legacy_acl.mfid = :project_mfid
            JOIN resourceacl canonical_acl
              ON canonical_acl.accessgroup_id = canonical.id
             AND canonical_acl.mfid = :project_mfid
            WHERE legacy.group_name = :legacy_group
        """), {"legacy_group": LEGACY_GROUP, "project_mfid": PROJECT_MFID}).one_or_none()
        expected = (
            state is not None
            and state.legacy_group_type == "custom"
            and state.canonical_group_type == "project"
            and state.legacy_permission == "owner"
            and state.canonical_permission == "owner"
            and state.request_count == 0
        )
        if not expected:
            print(f"REFUSING TO RUN: reviewed duplicate-owner state changed: {state!r}")
            return 2

        memberships = session.execute(text("""
            SELECT u.orcid, u.username, link.role::text
            FROM useraccessgrouplink link
            JOIN "user" u ON u.id = link.user_id
            WHERE link.accessgroup_id = :group_id
            ORDER BY u.orcid
        """), {"group_id": state.legacy_group_id}).all()
        other_acls = session.execute(text("""
            SELECT count(*)
            FROM resourceacl
            WHERE accessgroup_id = :group_id
              AND mfid <> :project_mfid
        """), {"group_id": state.legacy_group_id, "project_mfid": PROJECT_MFID}).scalar_one()
        if other_acls:
            print(f"REFUSING TO RUN: {LEGACY_GROUP!r} has {other_acls} unreviewed ACLs.")
            return 2

        print(f"group         : {LEGACY_GROUP!r} ({state.legacy_group_id})")
        print(f"project       : {PROJECT_MFID!r}")
        print(f"memberships   : {memberships!r}")
        print("action        : delete one owner ACL, all memberships, and the legacy group")
        if not args.commit:
            print("dry run only: nothing written")
            return 0

        acl_deleted = session.execute(text("""
            DELETE FROM resourceacl
            WHERE accessgroup_id = :group_id
              AND mfid = :project_mfid
              AND permission = 'owner'
        """), {"group_id": state.legacy_group_id, "project_mfid": PROJECT_MFID}).rowcount
        memberships_deleted = session.execute(text("""
            DELETE FROM useraccessgrouplink WHERE accessgroup_id = :group_id
        """), {"group_id": state.legacy_group_id}).rowcount
        group_deleted = session.execute(text("""
            DELETE FROM accessgroup
            WHERE id = :group_id
              AND group_name = :group_name
              AND group_type = 'custom'
        """), {"group_id": state.legacy_group_id, "group_name": LEGACY_GROUP}).rowcount
        if acl_deleted != 1 or memberships_deleted != len(memberships) or group_deleted != 1:
            session.rollback()
            print(
                "REFUSING TO COMMIT: unexpected delete counts "
                f"ACL={acl_deleted}, memberships={memberships_deleted}, group={group_deleted}."
            )
            return 2
        session.commit()
        print(
            f"committed: removed {LEGACY_GROUP!r}, one ACL, "
            f"and {memberships_deleted} membership(s)"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
