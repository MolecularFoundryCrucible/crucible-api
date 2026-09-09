"""Audit resource timestamps before converting idtype columns to timestamptz.

This script is read-only.

Usage:
    DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20260908_audit_resource_timestamps.py
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$"
)


def classify_timestamp(value):
    if value is None:
        return "null"
    if not isinstance(value, str) or TIMESTAMP_PATTERN.fullmatch(value) is None:
        return "invalid"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        offset = parsed.utcoffset()
    except (AttributeError, TypeError, ValueError):
        return "invalid"
    if offset is None:
        return "invalid"
    return "utc" if offset == timedelta(0) else "non_utc"


def main() -> int:
    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        counts = {
            field_name: {state: 0 for state in ("null", "invalid", "non_utc", "utc")}
            for field_name in ("creation_time", "modification_time")
        }
        samples = {
            field_name: {state: [] for state in ("null", "invalid", "non_utc")}
            for field_name in ("creation_time", "modification_time")
        }
        rows = session.execute(text("""
            SELECT mfid, creation_time, modification_time
            FROM idtype
            ORDER BY mfid
        """).execution_options(stream_results=True))
        for row in rows:
            for field_name in ("creation_time", "modification_time"):
                state = classify_timestamp(getattr(row, field_name))
                counts[field_name][state] += 1
                if state in samples[field_name] and len(samples[field_name][state]) < 20:
                    samples[field_name][state].append(row.mfid)

        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")
        for field_name in ("creation_time", "modification_time"):
            print(f"{field_name}:")
            for state in ("null", "invalid", "non_utc", "utc"):
                print(f"  {state:9}: {counts[field_name][state]}")
                if state in samples[field_name] and samples[field_name][state]:
                    print(f"    sample MFIDs: {samples[field_name][state]!r}")

        blocking = (
            counts["creation_time"]["null"]
            or counts["creation_time"]["invalid"]
            or counts["modification_time"]["invalid"]
        )
        if blocking:
            print("BLOCKED: creation times must be present and all non-null timestamps must be valid")
            return 1
        if counts["modification_time"]["null"]:
            print("PASS WITH REPAIR: migration will set missing modification times from creation times")
        else:
            print("PASS: resource timestamps satisfy migration preconditions")
        if counts["creation_time"]["non_utc"] or counts["modification_time"]["non_utc"]:
            print("INFO: non-UTC offsets will be preserved as instants and normalized by TIMESTAMPTZ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
