"""Remove each instrument operator group's grant on its own instrument record.

Instrument creation no longer writes this grant. Operator groups hold service accounts only, which are capped at contributor, so the grant only let bound service accounts attach scientific metadata to the instrument record. Grants on datasets filed against an instrument are not touched.

The dry run is read-only. Commit requires --yes-this-is-production on the production database.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20261005_remove_instrument_group_record_grants.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20261005_remove_instrument_group_record_grants.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"

_SELF_GRANTS = """
    FROM resourceacl acl
    JOIN accessgroup ag ON ag.id = acl.accessgroup_id
    JOIN instrument i ON i.unique_id = acl.mfid
    WHERE ag.group_name = i.unique_id
      AND ag.group_type = 'instrument'
"""


def find_self_grants(session) -> list:
    return session.execute(text(f"""
        SELECT i.unique_id, i.instrument_id, acl.permission
        {_SELF_GRANTS}
        ORDER BY i.unique_id
    """)).all()


def delete_self_grants(session) -> int:
    return session.execute(text(f"""
        DELETE FROM resourceacl
        WHERE (mfid, accessgroup_id) IN (
            SELECT acl.mfid, acl.accessgroup_id
            {_SELF_GRANTS}
        )
    """)).rowcount


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

        grants = find_self_grants(session)
        print(f"self grants   : {len(grants)}")
        for row in grants[:50]:
            print(f"  {row.unique_id} ({row.instrument_id}): {row.permission}")
        if not args.commit:
            print("dry run only: nothing written")
            return 0

        deleted = delete_self_grants(session)
        if deleted != len(grants):
            session.rollback()
            print(f"REFUSING TO COMMIT: expected {len(grants)} deletions, got {deleted}")
            return 2
        session.commit()
        print(f"committed: removed {deleted} instrument self grants")
    return 0


if __name__ == "__main__":
    sys.exit(main())
