import base64
import json

import pytest

from app.utils.cursor_utils import CursorError, decode_cursor, new_cursor


CURSOR_CONTEXT = {
    "kind": "dataset",
    "sort": "name",
    "direction": "asc",
    "filters": "filter-digest",
    "user_id": "user-1",
}


def _new_cursor():
    return new_cursor(
        kind="dataset",
        sort="name",
        direction="asc",
        primary="sample",
        unique_id="dataset-1",
        filters="filter-digest",
        user_id="user-1",
    )


def test_cursor_signing_key_rotation_accepts_retained_old_key(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEYS", json.dumps({"old": "old-secret"}))
    monkeypatch.setenv("CRUCIBLE_CURSOR_ACTIVE_KEY_ID", "old")
    old_cursor = _new_cursor()

    monkeypatch.setenv(
        "CRUCIBLE_CURSOR_SIGNING_KEYS",
        json.dumps({"old": "old-secret", "new": "new-secret"}),
    )
    monkeypatch.setenv("CRUCIBLE_CURSOR_ACTIVE_KEY_ID", "new")

    assert decode_cursor(old_cursor, **CURSOR_CONTEXT).kid == "old"
    assert decode_cursor(_new_cursor(), **CURSOR_CONTEXT).kid == "new"


def test_cursor_rejects_removed_key_and_invalid_configuration(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEYS", json.dumps({"old": "old-secret"}))
    monkeypatch.setenv("CRUCIBLE_CURSOR_ACTIVE_KEY_ID", "old")
    old_cursor = _new_cursor()

    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEYS", json.dumps({"new": "new-secret"}))
    monkeypatch.setenv("CRUCIBLE_CURSOR_ACTIVE_KEY_ID", "new")
    with pytest.raises(CursorError, match="Unknown cursor signing key"):
        decode_cursor(old_cursor, **CURSOR_CONTEXT)

    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEYS", "[]")
    with pytest.raises(RuntimeError, match="must map"):
        _new_cursor()


def test_cursor_is_compact_and_not_json_parseable():
    cursor = _new_cursor()
    payload = cursor.split(".", 2)[1]
    decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))

    with pytest.raises((json.JSONDecodeError, UnicodeDecodeError)):
        json.loads(decoded)
    assert len(cursor) < 150


def test_cursor_signature_binds_request_context():
    cursor = _new_cursor()

    with pytest.raises(CursorError, match="Invalid cursor or request context"):
        decode_cursor(cursor, **{**CURSOR_CONTEXT, "direction": "desc"})
