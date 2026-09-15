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


AWARE_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?(Z|[+-]\d{2}(:\d{2})?)$"
)
NAIVE_TIMESTAMP_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d{1,6})?$"
)
DATE_ONLY_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COMPACT_DATE_PATTERN = re.compile(r"^\d{8}$")
UNDERSCORE_NAIVE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{6}$")
MERIDIEM_DATE_PATTERN = re.compile(r"^\d{8}_(am|pm)$")
MISSING_SENTINELS = {"", "NaN"}


def classify_timestamp(value):
    if value is None:
        return "null"
    if not isinstance(value, str):
        return "invalid"
    if value in MISSING_SENTINELS:
        return "missing_sentinel"
    if AWARE_TIMESTAMP_PATTERN.fullmatch(value) is not None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            offset = parsed.utcoffset()
        except (AttributeError, TypeError, ValueError):
            return "invalid"
        if offset is None:
            return "invalid"
        return "utc" if offset == timedelta(0) else "non_utc"
    if NAIVE_TIMESTAMP_PATTERN.fullmatch(value) is not None:
        try:
            parsed = datetime.fromisoformat(value)
        except (AttributeError, TypeError, ValueError):
            return "invalid"
        if parsed.utcoffset() is None:
            return "naive"
        return "invalid"
    if DATE_ONLY_PATTERN.fullmatch(value) is not None:
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return "invalid"
        return "date_only"
    if COMPACT_DATE_PATTERN.fullmatch(value) is not None:
        try:
            datetime.strptime(value, "%Y%m%d")
        except ValueError:
            return "invalid"
        return "date_only"
    if UNDERSCORE_NAIVE_PATTERN.fullmatch(value) is not None:
        try:
            datetime.strptime(value, "%Y-%m-%d_%H%M%S")
        except ValueError:
            return "invalid"
        return "naive"
    if MERIDIEM_DATE_PATTERN.fullmatch(value) is not None:
        try:
            datetime.strptime(value[:8], "%Y%m%d")
        except ValueError:
            return "invalid"
        return "meridiem_date"
    return "invalid"


def has_blocking_values(counts) -> bool:
    return bool(
        counts["creation_time"]["invalid"]
        or counts["modification_time"]["invalid"]
    )


def main() -> int:
    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        counts = {
            field_name: {state: 0 for state in ("null", "date_only", "naive", "meridiem_date", "missing_sentinel", "invalid", "non_utc", "utc")}
            for field_name in ("creation_time", "modification_time")
        }
        samples = {
            field_name: {state: [] for state in ("null", "date_only", "naive", "meridiem_date", "missing_sentinel", "invalid", "non_utc")}
            for field_name in ("creation_time", "modification_time")
        }
        sampled_values = {
            field_name: {state: set() for state in ("date_only", "meridiem_date", "missing_sentinel", "invalid")}
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
                value = getattr(row, field_name)
                if state in sampled_values[field_name]:
                    if value in sampled_values[field_name][state] or len(samples[field_name][state]) >= 20:
                        continue
                    sampled_values[field_name][state].add(value)
                    samples[field_name][state].append((row.mfid, value))
                elif state in samples[field_name] and len(samples[field_name][state]) < 20:
                    samples[field_name][state].append(row.mfid)

        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")
        for field_name in ("creation_time", "modification_time"):
            print(f"{field_name}:")
            for state in ("null", "date_only", "naive", "meridiem_date", "missing_sentinel", "invalid", "non_utc", "utc"):
                print(f"  {state:9}: {counts[field_name][state]}")
                if state in samples[field_name] and samples[field_name][state]:
                    label = "sample MFIDs" if state not in sampled_values[field_name] else "sample MFID/value pairs"
                    print(f"    {label}: {samples[field_name][state]!r}")

        if has_blocking_values(counts):
            print("BLOCKED: all non-null resource timestamps must match an approved conversion format")
            return 1
        has_migration_notes = False
        if counts["creation_time"]["null"]:
            print("PASS WITH LEGACY NULLS: missing creation times will remain NULL")
            has_migration_notes = True
        if counts["creation_time"]["naive"] or counts["modification_time"]["naive"]:
            print("PASS WITH PACIFIC ASSUMPTION: timezone-less legacy timestamps will be interpreted in America/Los_Angeles")
            has_migration_notes = True
        if counts["creation_time"]["date_only"] or counts["modification_time"]["date_only"]:
            print("PASS WITH PACIFIC MIDNIGHT ASSUMPTION: date-only legacy timestamps will use 00:00:00 in America/Los_Angeles")
            has_migration_notes = True
        if counts["creation_time"]["meridiem_date"] or counts["modification_time"]["meridiem_date"]:
            print("PASS WITH PACIFIC MERIDIEM ASSUMPTION: date-only _am values will use 00:00:00 and _pm values will use 12:00:00 in America/Los_Angeles")
            has_migration_notes = True
        if counts["creation_time"]["missing_sentinel"] or counts["modification_time"]["missing_sentinel"]:
            print("PASS WITH MISSING-SENTINEL CLEANUP: empty strings and NaN values will become NULL")
            has_migration_notes = True
        if counts["modification_time"]["null"]:
            print("PASS WITH PARTIAL REPAIR: missing modification times will use creation time when available and otherwise remain NULL")
            has_migration_notes = True
        if not has_migration_notes:
            print("PASS: resource timestamps satisfy migration preconditions")
        if counts["creation_time"]["non_utc"] or counts["modification_time"]["non_utc"]:
            print("INFO: non-UTC offsets will be preserved as instants and normalized by TIMESTAMPTZ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
