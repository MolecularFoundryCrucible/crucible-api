"""Restore 17 reviewed legacy-only project memberships as contributors.

The reviewed users exist in userprojectlink but not in the canonical project
access group. The admin_developer membership in 10k_perovskites is deliberately
excluded. The script requires the exact projects, users, legacy links, and
canonical project groups listed below. Existing contributor memberships are
accepted; any other existing role blocks the run.

The script defaults to a read-only dry run. A production write requires both
--commit and --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260901_restore_confirmed_project_memberships.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260901_restore_confirmed_project_memberships.py --commit --yes-this-is-production
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"
ROLE = "contributor"
TARGETS = (
    ("alphafold-demo", "0tmp130wp9yfs000t932tmen8w", "0000-0002-4746-879X"),
    ("alphafold_for_microelectronics", "0tmp130wp9v5v000w88nks7jsg", "0000-0002-4746-879X"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0002-0544-6963"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0002-0723-8068"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0002-5252-6157"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0002-8941-5207"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0002-9561-7980"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0003-0313-3565"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0003-2137-8726"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0003-3066-4791"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0000-0003-4004-2558"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0009-0003-1271-2486"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0009-0003-1871-5215"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0009-0004-3993-0798"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0009-0005-5667-2089"),
    ("Chu_Hall_instruments", "0tmp130x4sxtx0008hhyvppf0r", "0009-0005-9155-7262"),
    ("MFP10688", "0tmp130wshveh000s5me2zbdp8", "0000-0001-6402-3752"),
)


TARGET_CTE = """
    target AS (
        SELECT *
        FROM jsonb_to_recordset(CAST(:targets AS jsonb)) AS value(
            project_id text,
            project_mfid text,
            orcid text
        )
    )
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="restore the reviewed memberships")
    parser.add_argument("--yes-this-is-production", action="store_true")
    args = parser.parse_args()

    targets = json.dumps([
        {"project_id": project_id, "project_mfid": project_mfid, "orcid": orcid}
        for project_id, project_mfid, orcid in TARGETS
    ])
    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()"), {}).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (read only)'}")
        print(f"targets       : {len(TARGETS)} at {ROLE!r}")

        if args.commit and (database != PRODUCTION_DB or not args.yes_this_is_production):
            print("REFUSING TO COMMIT: production database and confirmation flag are required")
            return 2

        rows = session.execute(text(f"""
            WITH {TARGET_CTE}
            SELECT
                target.project_id,
                target.project_mfid,
                target.orcid,
                project.id AS project_db_id,
                user_row.id AS user_db_id,
                access_group.id AS accessgroup_id,
                legacy.user_id IS NOT NULL AS has_legacy_link,
                canonical.role::text AS current_role
            FROM target
            LEFT JOIN project
              ON project.project_id = target.project_id
             AND project.unique_id = target.project_mfid
            LEFT JOIN "user" user_row ON user_row.orcid = target.orcid
            LEFT JOIN userprojectlink legacy
              ON legacy.project_id = target.project_id
             AND legacy.user_id = user_row.id
            LEFT JOIN accessgroup access_group
              ON access_group.group_name = target.project_mfid
             AND access_group.group_type = 'project'
            LEFT JOIN useraccessgrouplink canonical
              ON canonical.user_id = user_row.id
             AND canonical.accessgroup_id = access_group.id
            ORDER BY target.project_id, target.orcid
        """), {"targets": targets}).all()

        invalid = [
            row for row in rows
            if row.project_db_id is None
            or row.user_db_id is None
            or row.accessgroup_id is None
            or not row.has_legacy_link
            or row.current_role not in (None, ROLE)
        ]
        if len(rows) != len(TARGETS) or invalid:
            print(f"REFUSING TO RUN: expected {len(TARGETS)} valid targets, found {len(rows)}")
            for row in invalid:
                print(f"  invalid: {row!r}")
            return 2

        pending = [row for row in rows if row.current_role is None]
        existing = [row for row in rows if row.current_role == ROLE]
        print(f"pending       : {len(pending)}")
        print(f"already set   : {len(existing)}")
        for row in pending:
            print(f"  {row.project_id}: {row.orcid}")
        if not args.commit:
            print("dry run only: nothing written")
            return 0

        inserted = session.execute(text(f"""
            WITH {TARGET_CTE}
            INSERT INTO useraccessgrouplink (user_id, accessgroup_id, role)
            SELECT user_row.id, access_group.id, CAST(:role AS resource_role)
            FROM target
            JOIN project
              ON project.project_id = target.project_id
             AND project.unique_id = target.project_mfid
            JOIN "user" user_row ON user_row.orcid = target.orcid
            JOIN userprojectlink legacy
              ON legacy.project_id = target.project_id
             AND legacy.user_id = user_row.id
            JOIN accessgroup access_group
              ON access_group.group_name = target.project_mfid
             AND access_group.group_type = 'project'
            ON CONFLICT (user_id, accessgroup_id) DO NOTHING
        """), {"targets": targets, "role": ROLE}).rowcount
        if inserted != len(pending):
            session.rollback()
            print(f"REFUSING TO COMMIT: expected {len(pending)} inserts, got {inserted}")
            return 2

        session.commit()
        print(f"committed: restored {inserted} project memberships at {ROLE!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
