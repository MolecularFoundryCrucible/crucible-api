from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator, PlainSerializer, WithJsonSchema


def normalize_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def serialize_utc_datetime(value: datetime) -> str:
    return normalize_utc_datetime(value).isoformat(
        timespec="microseconds",
    ).replace("+00:00", "Z")


UtcDatetime = Annotated[
    datetime,
    AfterValidator(normalize_utc_datetime),
    PlainSerializer(serialize_utc_datetime, return_type=str, when_used="json"),
    WithJsonSchema({"type": "string", "format": "date-time"}, mode="serialization"),
]
