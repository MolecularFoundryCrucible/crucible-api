import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from google.protobuf.message import DecodeError
from google.protobuf.struct_pb2 import ListValue
from pydantic import BaseModel, ConfigDict

from app.utils.auth_utils import SECRET_KEY


CURSOR_PREFIX = "c1."


class CursorError(ValueError):
    pass


class ResourceCursor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: int
    kid: str
    kind: str
    sort: str
    direction: str
    is_null: bool
    primary: Optional[str]
    unique_id: str
    filters: str
    principal: str


def _signing_keys() -> tuple[str, dict[str, bytes]]:
    configured = os.getenv("CRUCIBLE_CURSOR_SIGNING_KEYS")
    if configured:
        try:
            values = json.loads(configured)
        except json.JSONDecodeError as exc:
            raise RuntimeError("CRUCIBLE_CURSOR_SIGNING_KEYS must be a JSON object") from exc
        if not isinstance(values, dict) or not values or not all(
            isinstance(key_id, str) and key_id and isinstance(secret, str) and secret
            for key_id, secret in values.items()
        ):
            raise RuntimeError("CRUCIBLE_CURSOR_SIGNING_KEYS must map non-empty key IDs to non-empty secrets")
        keys = {key_id: secret.encode("utf-8") for key_id, secret in values.items()}
    else:
        keys = {"v1": os.getenv("CRUCIBLE_CURSOR_SIGNING_KEY", SECRET_KEY).encode("utf-8")}
    active_key_id = os.getenv("CRUCIBLE_CURSOR_ACTIVE_KEY_ID", "v1")
    if active_key_id not in keys:
        raise RuntimeError("CRUCIBLE_CURSOR_ACTIVE_KEY_ID is not present in CRUCIBLE_CURSOR_SIGNING_KEYS")
    return active_key_id, keys


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _normalize_filter_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_filter_value(item) for key, item in sorted(value.items())}
    if isinstance(value, list):
        normalized = [_normalize_filter_value(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _digest(label: str, value: bytes) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), label.encode("utf-8") + b":" + value, hashlib.sha256).hexdigest()


def principal_fingerprint(user_id: str) -> str:
    return _digest("principal", user_id.encode("utf-8"))


def filter_fingerprint(filters: dict[str, Any]) -> str:
    return _digest("filters", _canonical_json(_normalize_filter_value(filters)))


def _encode_payload(cursor: ResourceCursor) -> bytes:
    message = ListValue()
    message.values.add().string_value = cursor.kid
    primary = message.values.add()
    if cursor.primary is None:
        primary.null_value = 0
    else:
        primary.string_value = cursor.primary
    message.values.add().string_value = cursor.unique_id
    return message.SerializeToString(deterministic=True)


def _decode_payload(payload: bytes) -> tuple[str, Optional[str], str]:
    message = ListValue()
    try:
        message.ParseFromString(payload)
    except DecodeError as exc:
        raise CursorError("Invalid cursor") from exc
    if len(message.values) != 3:
        raise CursorError("Invalid cursor payload")
    key_value, primary_value, unique_id_value = message.values
    if key_value.WhichOneof("kind") != "string_value" or not key_value.string_value:
        raise CursorError("Invalid cursor payload")
    primary_kind = primary_value.WhichOneof("kind")
    if primary_kind == "null_value":
        primary = None
    elif primary_kind == "string_value":
        primary = primary_value.string_value
    else:
        raise CursorError("Invalid cursor payload")
    if unique_id_value.WhichOneof("kind") != "string_value" or not unique_id_value.string_value:
        raise CursorError("Invalid cursor payload")
    return key_value.string_value, primary, unique_id_value.string_value


def _signature_input(cursor: ResourceCursor, payload: bytes) -> bytes:
    context = _canonical_json({
        "direction": cursor.direction,
        "filters": cursor.filters,
        "kind": cursor.kind,
        "principal": cursor.principal,
        "sort": cursor.sort,
    })
    return CURSOR_PREFIX.encode("ascii") + len(payload).to_bytes(4, "big") + payload + context


def encode_cursor(cursor: ResourceCursor) -> str:
    payload = _encode_payload(cursor)
    _, keys = _signing_keys()
    key = keys.get(cursor.kid)
    if key is None:
        raise RuntimeError(f"Unknown cursor signing key ID: {cursor.kid}")
    signature = hmac.new(key, _signature_input(cursor, payload), hashlib.sha256).digest()
    return f"{CURSOR_PREFIX}{_b64encode(payload)}.{_b64encode(signature)}"


def decode_cursor(
    value: str,
    *,
    kind: str,
    sort: str,
    direction: str,
    filters: str,
    user_id: str,
) -> Optional[ResourceCursor]:
    if not value.startswith(CURSOR_PREFIX):
        return None
    try:
        payload_value, signature_value = value[len(CURSOR_PREFIX):].split(".", 1)
        payload = _b64decode(payload_value)
        signature = _b64decode(signature_value)
        key_id, primary, unique_id = _decode_payload(payload)
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise CursorError("Invalid cursor") from exc
    _, keys = _signing_keys()
    key = keys.get(key_id)
    if key is None:
        raise CursorError("Unknown cursor signing key")
    cursor = ResourceCursor(
        v=1,
        kid=key_id,
        kind=kind,
        sort=sort,
        direction=direction,
        is_null=primary is None,
        primary=primary,
        unique_id=unique_id,
        filters=filters,
        principal=principal_fingerprint(user_id),
    )
    expected = hmac.new(key, _signature_input(cursor, payload), hashlib.sha256).digest()
    if not hmac.compare_digest(signature, expected):
        raise CursorError("Invalid cursor or request context")
    return cursor


def validate_cursor(
    value: str,
    *,
    kind: str,
    sort: str,
    direction: str,
    filters: str,
    user_id: str,
) -> Optional[ResourceCursor]:
    cursor = decode_cursor(
        value,
        kind=kind,
        sort=sort,
        direction=direction,
        filters=filters,
        user_id=user_id,
    )
    if cursor is None:
        if sort != "unique_id":
            raise CursorError("Legacy cursors cannot be combined with explicit sorting")
        return None
    return cursor


def cursor_primary_value(cursor: ResourceCursor) -> Any:
    if cursor.is_null:
        if cursor.primary is not None:
            raise CursorError("Invalid null cursor value")
        return None
    if cursor.primary is None:
        raise CursorError("Missing cursor sort value")
    if cursor.sort in {"created", "updated", "activity", "last_active"}:
        try:
            value = datetime.fromisoformat(cursor.primary)
        except ValueError as exc:
            raise CursorError("Invalid cursor timestamp") from exc
        if value.tzinfo is None:
            raise CursorError("Invalid cursor timestamp")
        return value
    return cursor.primary


def new_cursor(
    *,
    kind: str,
    sort: str,
    direction: str,
    primary: Any,
    unique_id: str,
    filters: str,
    user_id: str,
) -> str:
    if isinstance(primary, datetime):
        if primary.tzinfo is None:
            primary = primary.replace(tzinfo=timezone.utc)
        primary = primary.astimezone(timezone.utc).isoformat()
    elif primary is not None:
        primary = str(primary)
    active_key_id, _ = _signing_keys()
    return encode_cursor(ResourceCursor(
        v=1,
        kid=active_key_id,
        kind=kind,
        sort=sort,
        direction=direction,
        is_null=primary is None,
        primary=primary,
        unique_id=unique_id,
        filters=filters,
        principal=principal_fingerprint(user_id),
    ))
