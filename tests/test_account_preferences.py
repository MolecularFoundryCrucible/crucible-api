import json

import pytest
from sqlmodel import Session, select

from app.models.core_models import User, UserPreferences

ME = "0000-0002-0000-0001"
OTHER = "0000-0002-0000-0002"
URL = "/account/preferences"


@pytest.fixture
def me(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(User(first_name="Pref", last_name="Erence", email=None, unique_id=ME, username="pref"))
        session.add(User(first_name="Oth", last_name="Er", email=None, unique_id=OTHER, username="other"))
        session.commit()
    auth_as(ME)
    return client


def patch(client, body, if_match=None, content_type="application/merge-patch+json"):
    headers = {"Content-Type": content_type}
    if if_match is not None:
        headers["If-Match"] = if_match
    return client.patch(URL, content=json.dumps(body), headers=headers)


def test_get_without_row_returns_defaults(me):
    response = me.get(URL)
    assert response.status_code == 200
    assert response.headers["ETag"] == '"p0"'
    assert response.json() == {"schema_version": 1, "revision": 0, "updated_at": None, "preferences": {}}


def test_patch_creates_and_merges(me):
    response = patch(me, {"theme": "dark", "hub": {"graph": {"orientation": "vertical", "associations": True}}})
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"p1"'
    assert response.json()["revision"] == 1
    assert response.json()["updated_at"] is not None

    response = patch(me, {"default_record_kind": "samples", "hub": {"graph": {"associations": None}, "home": {"recent_scope": "mine"}}}, content_type="application/json")
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"p2"'
    assert response.json()["preferences"] == {
        "theme": "dark",
        "default_record_kind": "samples",
        "hub": {"graph": {"orientation": "vertical"}, "home": {"recent_scope": "mine"}},
    }

    response = patch(me, {"theme": None, "hub": None})
    assert response.json()["preferences"] == {"default_record_kind": "samples"}
    assert me.get(URL).json()["preferences"] == {"default_record_kind": "samples"}


def test_if_match(me):
    assert patch(me, {"theme": "light"}, if_match='"p0"').status_code == 200
    stale = patch(me, {"theme": "dark"}, if_match='"p0"')
    assert stale.status_code == 412
    assert stale.json()["detail"]["code"] == "etag_mismatch"
    assert stale.json()["detail"]["etag"] == '"p1"'
    assert patch(me, {"theme": "dark"}, if_match='"p1"').headers["ETag"] == '"p2"'
    assert patch(me, {"theme": "system"}, if_match="*").headers["ETag"] == '"p3"'
    assert me.get(URL).json()["preferences"] == {"theme": "system"}


def test_if_match_on_missing_row(me):
    response = patch(me, {"theme": "dark"}, if_match='"p4"')
    assert response.status_code == 412
    assert response.json()["detail"]["etag"] == '"p0"'


@pytest.mark.parametrize("body", [
    {"theme": "blue"},
    {"default_record_kind": "projects"},
    {"unknown": 1},
    {"hub": "not-an-object"},
    {"hub": {"blob": "x" * 5000}},
    {"theme": ["dark"]},
])
def test_invalid_documents_are_rejected(me, body):
    response = patch(me, body)
    assert response.status_code == 422, response.text
    assert me.get(URL).json()["revision"] == 0


def test_non_object_patch_is_rejected(me):
    assert patch(me, ["theme"]).status_code == 422


def test_rejected_patch_leaves_document_unchanged(me):
    patch(me, {"theme": "dark"})
    assert patch(me, {"theme": "blue"}).status_code == 422
    assert me.get(URL).json() | {"updated_at": None} == {"schema_version": 1, "revision": 1, "updated_at": None, "preferences": {"theme": "dark"}}


def test_preferences_are_caller_scoped(me, auth_as, db_engine):
    patch(me, {"theme": "dark"})
    auth_as(OTHER)
    assert me.get(URL).json()["preferences"] == {}
    patch(me, {"theme": "light"})
    with Session(db_engine) as session:
        rows = {row.user_id: row.preferences for row in session.exec(select(UserPreferences)).all()}
        users = {user.unique_id: user.id for user in session.exec(select(User)).all()}
    assert rows == {users[ME]: {"theme": "dark"}, users[OTHER]: {"theme": "light"}}


def test_unknown_caller_gets_404(me, auth_as):
    auth_as("0000-0002-0000-0099")
    assert me.get(URL).status_code == 404
    assert patch(me, {"theme": "dark"}).status_code == 404
