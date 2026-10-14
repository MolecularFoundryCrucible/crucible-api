"""Backfill activity timestamps added by migration b644cf2e7240.

- user.last_active_at: the user's latest Hub session refresh. Users who only use API keys stay empty until their next request.
- project.last_activity_at: the latest modification time of the project's datasets and samples.
- instrument.last_activity_at: the latest modification time of datasets recorded on the instrument.
- apitoken.last_used_at is not backfilled; there is no record of past use.

Only empty values are filled. Prints the number of rows each step would change. Dry run by default: the transaction is rolled back unless --commit is passed.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20261014_backfill_activity_timestamps.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20261014_backfill_activity_timestamps.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine

STEPS = {
    "user.last_active_at": """
        UPDATE "user" u SET last_active_at = latest.at
        FROM (SELECT user_unique_id, max(last_refreshed_at) AS at FROM hubtokenfamily GROUP BY user_unique_id) latest
        WHERE u.orcid = latest.user_unique_id AND u.last_active_at IS NULL
    """,
    "project.last_activity_at": """
        UPDATE project p SET last_activity_at = latest.at
        FROM (
            SELECT records.project_mfid, max(coalesce(i.modification_time, i.creation_time)) AS at
            FROM (SELECT unique_id, project_mfid FROM dataset UNION ALL SELECT unique_id, project_mfid FROM sample) records
            JOIN idtype i ON i.mfid = records.unique_id
            WHERE records.project_mfid IS NOT NULL
            GROUP BY records.project_mfid
        ) latest
        WHERE p.unique_id = latest.project_mfid AND p.last_activity_at IS NULL AND latest.at IS NOT NULL
    """,
    "instrument.last_activity_at": """
        UPDATE instrument ins SET last_activity_at = latest.at
        FROM (
            SELECT d.instrument_mfid, max(coalesce(i.modification_time, i.creation_time)) AS at
            FROM dataset d JOIN idtype i ON i.mfid = d.unique_id
            WHERE d.instrument_mfid IS NOT NULL
            GROUP BY d.instrument_mfid
        ) latest
        WHERE ins.unique_id = latest.instrument_mfid AND ins.last_activity_at IS NULL AND latest.at IS NOT NULL
    """,
}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", action="store_true", help="Write the changes; without it the transaction is rolled back.")
    args = parser.parse_args()
    with Session(get_engine()) as session:
        for name, statement in STEPS.items():
            print(f"{name}: {session.connection().execute(text(statement)).rowcount} rows")
        if args.commit:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run, rolled back")


if __name__ == "__main__":
    main()
