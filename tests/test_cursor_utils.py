import json

import pytest

from app.utils.cursor_utils import CursorError, decode_cursor, new_cursor


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

    assert decode_cursor(old_cursor).kid == "old"
    assert decode_cursor(_new_cursor()).kid == "new"


def test_cursor_rejects_removed_key_and_invalid_configuration(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEYS", json.dumps({"old": "old-secret"}))
    monkeypatch.setenv("CRUCIBLE_CURSOR_ACTIVE_KEY_ID", "old")
    old_cursor = _new_cursor()

    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEYS", json.dumps({"new": "new-secret"}))
    monkeypatch.setenv("CRUCIBLE_CURSOR_ACTIVE_KEY_ID", "new")
    with pytest.raises(CursorError, match="Unknown cursor signing key"):
        decode_cursor(old_cursor)

    monkeypatch.setenv("CRUCIBLE_CURSOR_SIGNING_KEYS", "[]")
    with pytest.raises(RuntimeError, match="must map"):
        _new_cursor()
