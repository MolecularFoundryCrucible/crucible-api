"""Audit resource timestamps before converting idtype columns to timestamptz.

This script is read-only.

Usage:
    DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20260908_audit_resource_timestamps.py
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
        row = session.execute(text(r"""
            SELECT
                current_database() AS database,
                count(*) FILTER (
                    WHERE creation_time IS NOT NULL
                    AND creation_time !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$'
                ) AS invalid_creation_times,
                count(*) FILTER (
                    WHERE modification_time IS NOT NULL
                    AND modification_time !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$'
                ) AS invalid_modification_times,
                ARRAY(
                    SELECT mfid
                    FROM idtype
                    WHERE
                        (creation_time IS NOT NULL AND creation_time !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$')
                        OR
                        (modification_time IS NOT NULL AND modification_time !~ '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$')
                    ORDER BY mfid
                    LIMIT 20
                ) AS invalid_sample
            FROM idtype
        """)).one()

        print(f"connected to  : {row.database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")
        print(f"invalid creation timestamps    : {row.invalid_creation_times}")
        print(f"invalid modification timestamps: {row.invalid_modification_times}")
        if row.invalid_sample:
            print(f"sample MFIDs: {list(row.invalid_sample)!r}")
        if row.invalid_creation_times or row.invalid_modification_times:
            print("BLOCKED: resource timestamps require repair before migration")
            return 1
        print("PASS: resource timestamps include explicit timezones")
    return 0


if __name__ == "__main__":
    sys.exit(main())
