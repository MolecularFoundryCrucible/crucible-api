import importlib.util
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from app.models.core_models import Resource
from app.models.timestamps import UtcDatetime


AUDIT_PATH = Path(__file__).parents[1] / "migrations/data/20260908_audit_resource_timestamps.py"
AUDIT_SPEC = importlib.util.spec_from_file_location("audit_resource_timestamps", AUDIT_PATH)
AUDIT_MODULE = importlib.util.module_from_spec(AUDIT_SPEC)
AUDIT_SPEC.loader.exec_module(AUDIT_MODULE)


class TimestampResponse(BaseModel):
    value: UtcDatetime


def test_utc_timestamp_serialization_is_canonical():
    response = TimestampResponse(
        value=datetime.fromisoformat("2026-09-09T05:06:07.123456-07:00"),
    )

    assert response.value == datetime(2026, 9, 9, 12, 6, 7, 123456, tzinfo=timezone.utc)
    assert response.model_dump(mode="json")["value"] == "2026-09-09T12:06:07.123456Z"


def test_naive_database_timestamp_is_interpreted_as_utc():
    response = TimestampResponse(value=datetime(2026, 9, 9, 12, 6, 7))

    assert response.model_dump(mode="json")["value"] == "2026-09-09T12:06:07.000000Z"


def test_resource_lifecycle_timestamps_allow_legacy_nulls_with_database_defaults():
    for field_name in ("creation_time", "modification_time"):
        column = Resource.__table__.columns[field_name]
        assert column.nullable is True
        assert str(column.server_default.arg) == "now()"


def test_timestamp_audit_classifies_standard_offsets_and_invalid_values():
    classify = AUDIT_MODULE.classify_timestamp

    assert classify(None) == "null"
    assert classify("2026-09-09T12:06:07Z") == "utc"
    assert classify("2026-09-09T12:06:07.123456+00:00") == "utc"
    assert classify("2026-04-21 18:20:33.43427+00") == "utc"
    assert classify("2026-09-09T05:06:07-07:00") == "non_utc"
    assert classify("2026-09-09T12:06:07") == "naive"
    assert classify("2026-09-09 12:06:07") == "naive"
    assert classify("2026-99-99T12:06:07+00:00") == "invalid"


def test_timestamp_audit_classifies_approved_legacy_formats():
    classify = AUDIT_MODULE.classify_timestamp

    assert classify("2025-02-24") == "date_only"
    assert classify("20250704") == "date_only"
    assert classify("2026-03-02_015757") == "naive"
    assert classify("20251209_am") == "meridiem_date"
    assert classify("20260323_pm") == "meridiem_date"
    assert classify("") == "missing_sentinel"
    assert classify("NaN") == "missing_sentinel"
    assert classify("20260231") == "invalid"
    assert classify("2026-03-02_256100") == "invalid"


def test_timestamp_audit_allows_nulls_but_blocks_invalid_values():
    counts = {
        "creation_time": {"null": 10, "naive": 3, "invalid": 0, "non_utc": 0, "utc": 5},
        "modification_time": {"null": 8, "naive": 2, "invalid": 0, "non_utc": 0, "utc": 7},
    }

    assert AUDIT_MODULE.has_blocking_values(counts) is False

    counts["creation_time"]["invalid"] = 1
    assert AUDIT_MODULE.has_blocking_values(counts) is True
