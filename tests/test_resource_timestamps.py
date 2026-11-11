from datetime import datetime, timezone

from pydantic import BaseModel

from app.tables import Resource
from app.models.timestamps import UtcDatetime


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
