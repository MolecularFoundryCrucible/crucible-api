from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session, select

from app.auth.api_keys import MAX_ACTIVE_KEYS, key_hash, store_key
from app.models.core_models import ApiKey, ApiToken, User
from app.utils import auth_utils
from app.utils.permissions import PlatformRole

ADA = "0tmnyhfztdwc1000w2kspzsnh0"
BOB = "0tmnyhfztdwc1000w2kspzsnh1"
ADMIN = "0tmnyhfztdwc1000w2kspzsnh9"


@pytest.fixture
def people(db_engine, use_engine):
    use_engine(db_engine)
    with auth_utils._auth_cache_lock:
        auth_utils._auth_cache.clear()
    with Session(db_engine) as session:
        for unique_id, username, role in ((ADA, "ada", "none"), (BOB, "bob", "none"), (ADMIN, "boss", "admin")):
            session.add(User(unique_id=unique_id, username=username, first_name="A", last_name="B", email=None, platform_role=role))
        session.commit()
    return db_engine


def user_id(engine, unique_id):
    with Session(engine) as session:
        return session.exec(select(User.id).where(User.unique_id == unique_id)).one()


def as_hub(auth_as, unique_id, *, age=timedelta(seconds=5), role=PlatformRole.NONE):
    auth_as(unique_id, platform_role=role, credential="hub", authenticated_at=datetime.now(timezone.utc) - age)


def authorize(key):
    return auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=key))


def create(client, name="laptop", **body):
    return client.post("/account/apikeys", json={"name": name, **body})


def test_created_key_is_shown_once_with_its_prefix_and_works(client, auth_as, people):
    as_hub(auth_as, ADA)
    response = create(client)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["api_key"].startswith("crucible_") and body["last4"] == body["api_key"][-4:]
    assert (body["name"], body["status"]) == ("laptop", "active")
    assert response.headers["cache-control"] == "no-store"
    assert authorize(body["api_key"]).user_id == ADA
    listed = client.get("/account/apikeys").json()["items"]
    assert [(item["id"], item["last4"]) for item in listed] == [(body["id"], body["last4"])]
    assert body["api_key"] not in str(listed)
    with Session(people) as session:
        assert session.exec(select(ApiToken)).all() == []


def test_api_key_cannot_create_a_key_and_a_stale_session_signs_in_again(client, auth_as, people):
    auth_as(ADA)
    assert create(client).json()["detail"]["code"] == "hub_session_required"
    as_hub(auth_as, ADA, age=timedelta(hours=1))
    assert create(client).json()["detail"]["code"] == "insufficient_user_authentication"


def test_key_cap_of_ten(client, auth_as, people):
    as_hub(auth_as, ADA)
    for number in range(MAX_ACTIVE_KEYS):
        assert create(client, f"key {number}").status_code == 201
    refused = create(client, "one too many")
    assert (refused.status_code, refused.json()["detail"]["code"]) == (409, "too_many_keys")
    first = client.get("/account/apikeys").json()["items"][-1]["id"]
    assert client.delete(f"/account/apikeys/{first}").status_code == 204
    assert create(client, "after revoking one").status_code == 201


def test_expiry_must_be_in_the_next_year(client, auth_as, people):
    as_hub(auth_as, ADA)
    now = datetime.now(timezone.utc)
    for when in (now - timedelta(days=1), now + timedelta(days=400)):
        assert create(client, expires_at=when.isoformat()).status_code == 422
    chosen = create(client, expires_at=(now + timedelta(days=30)).isoformat()).json()
    assert chosen["expires_at"].startswith((now + timedelta(days=30)).date().isoformat())


def test_revoked_key_stops_working_and_is_listed_as_inactive(client, auth_as, people):
    as_hub(auth_as, ADA)
    body = create(client).json()
    assert authorize(body["api_key"]).user_id == ADA
    auth_as(ADA)
    assert client.delete(f"/account/apikeys/{body['id']}").status_code == 204
    with pytest.raises(HTTPException) as caught:
        authorize(body["api_key"])
    assert caught.value.status_code == 401
    assert client.get("/account/apikeys").json()["items"] == []
    assert [item["status"] for item in client.get("/account/apikeys", params={"state": "inactive"}).json()["items"]] == ["revoked"]


def test_someone_elses_key_cannot_be_revoked(client, auth_as, people):
    as_hub(auth_as, BOB)
    bobs = create(client).json()
    auth_as(ADA)
    assert client.delete(f"/account/apikeys/{bobs['id']}").status_code == 404
    assert authorize(bobs["api_key"]).user_id == BOB


def test_expired_key_is_refused_like_an_unknown_one(people):
    with Session(people) as session:
        store_key(session, user_id(people, ADA), "expired-key-0000", created_at=datetime.now(timezone.utc) - timedelta(days=400))
        session.commit()
    messages = []
    for key in ("expired-key-0000", "never-existed-0000"):
        with pytest.raises(HTTPException) as caught:
            authorize(key)
        messages.append((caught.value.status_code, caught.value.detail))
    assert messages[0] == messages[1] == (401, "Invalid or expired API key.")


def test_older_key_without_prefix_still_works_and_is_copied_on_first_use(people):
    with Session(people) as session:
        session.add(ApiToken(token="a" * 64, user_id=user_id(people, ADA), user_unique_id=ADA, access_group_id=ADA, token_created=datetime.now(timezone.utc).date().isoformat()))
        session.commit()
    assert authorize("a" * 64).user_id == ADA
    with Session(people) as session:
        assert session.exec(select(ApiKey.key_hash)).all() == [key_hash("a" * 64)]


def test_last_use_is_recorded_on_the_hashed_key(people):
    from app.services import activity

    with Session(people) as session:
        store_key(session, user_id(people, ADA), "used-key-0000")
        session.commit()
    activity.record_caller_activity(people, ADA, "used-key-0000")
    with Session(people) as session:
        assert session.exec(select(ApiKey.last_used_at)).one() is not None


def test_administrators_list_and_revoke_one_key(client, auth_as, people):
    as_hub(auth_as, ADA)
    adas = create(client).json()
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert [item["id"] for item in client.get(f"/users/{ADA}/apikeys").json()["items"]] == [adas["id"]]
    assert client.delete(f"/users/{BOB}/apikeys/{adas['id']}").status_code == 404
    assert client.delete(f"/users/{ADA}/apikeys/{adas['id']}").status_code == 204
    auth_as(BOB)
    client.headers.pop("Crucible-Privilege-Mode")
    assert client.get(f"/users/{ADA}/apikeys").status_code == 403


def test_post_users_creates_no_key(client, auth_as, people):
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    response = client.post("/users", json={"user_info": {"first_name": "New", "last_name": "Person", "username": "newcomer"}})
    assert response.status_code == 200, response.text
    with Session(people) as session:
        assert session.exec(select(ApiKey)).all() == [] and session.exec(select(ApiToken)).all() == []


def plain_key_only(engine, unique_id, token, created):
    with Session(engine) as session:
        session.add(ApiToken(token=token, user_id=user_id(engine, unique_id), user_unique_id=unique_id, access_group_id=unique_id, token_created=created))
        session.commit()


def test_regenerate_replaces_only_the_plain_key(client, auth_as, people):
    as_hub(auth_as, ADA)
    named = create(client).json()["api_key"]
    auth_as(ADA)
    first = client.post("/account/apikey/regenerate").json()["api_key"]
    second = client.post("/account/apikey/regenerate").json()["api_key"]
    assert authorize(named).user_id == ADA and authorize(second).user_id == ADA
    with pytest.raises(HTTPException):
        authorize(first)
    with Session(people) as session:
        assert len(session.exec(select(ApiKey).where(ApiKey.revoked_at == None)).all()) == 2  # noqa: E711


def test_status_counts_a_plain_key_not_yet_copied(client, auth_as, people):
    plain_key_only(people, ADA, "p" * 64, datetime.now(timezone.utc).date().isoformat())
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    as_hub(auth_as, ADMIN, role=PlatformRole.ADMIN)
    assert client.get(f"/users/{ADA}").json()["api_key_status"]["valid"] is True
    listed = {user["unique_id"]: user for user in client.get("/users", params={"limit": 50}).json()["items"]}
    assert listed[ADA]["api_key_status"]["valid"] is True
    assert client.post(f"/users/{ADA}/apikey").status_code == 409
    with Session(people) as session:
        assert session.exec(select(ApiKey)).all() == []


def test_status_prefers_a_working_key_over_a_newer_expired_one(client, auth_as, people):
    now = datetime.now(timezone.utc)
    with Session(people) as session:
        store_key(session, user_id(people, ADA), "working-key-0000", created_at=now - timedelta(days=10))
        store_key(session, user_id(people, ADA), "expired-key-0000", created_at=now - timedelta(days=1), expires_at=now - timedelta(hours=1))
        session.commit()
    as_hub(auth_as, ADA)
    response = client.get("/account/verify", headers={"Authorization": "Bearer chba_session"})
    assert response.json()["valid"] is True, response.text


def test_admin_revoke_all_stops_cached_named_keys_at_once(client, auth_as, people):
    as_hub(auth_as, ADA)
    named = create(client).json()["api_key"]
    assert authorize(named).user_id == ADA
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert client.delete(f"/users/{ADA}/apikey").status_code == 204
    with pytest.raises(HTTPException):
        authorize(named)


def test_a_used_hashed_key_counts_as_holding(people):
    from app.auth.linking import holdings

    with Session(people) as session:
        key = store_key(session, user_id(people, ADA), "used-key-0000")
        key.last_used_at = datetime.now(timezone.utc)
        session.commit()
        assert "1 used API keys" in holdings(session, session.get(User, user_id(people, ADA)))


def test_pending_filter_is_a_declared_parameter(client, auth_as, people):
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    response = client.get("/users", params={"pending": "true"})
    assert response.status_code == 200 and "deprecation" not in response.headers


def test_keys_can_be_renamed_and_the_older_routes_key_is_marked(client, auth_as, people):
    as_hub(auth_as, ADA)
    named = create(client).json()
    auth_as(ADA)
    client.post("/account/apikey/regenerate")
    listed = {item["name"]: item for item in client.get("/account/apikeys").json()["items"]}
    assert (listed["laptop"]["older_routes"], listed["Default key"]["older_routes"]) == (False, True)
    renamed = client.patch(f"/account/apikeys/{listed['Default key']['id']}", json={"name": "old scripts"}).json()
    assert (renamed["name"], renamed["older_routes"]) == ("old scripts", True)
    auth_as(BOB)
    assert client.patch(f"/account/apikeys/{named['id']}", json={"name": "mine now"}).status_code == 404


def test_a_copied_older_key_is_named(people):
    plain_key_only(people, ADA, "q" * 64, datetime.now(timezone.utc).date().isoformat())
    authorize("q" * 64)
    with Session(people) as session:
        assert session.exec(select(ApiKey.name)).all() == ["Default key"]


def test_inactive_list_shows_when_a_key_was_revoked(client, auth_as, people):
    as_hub(auth_as, ADA)
    key = create(client).json()
    assert key["revoked_at"] is None
    client.delete(f"/account/apikeys/{key['id']}")
    assert client.get("/account/apikeys", params={"state": "inactive"}).json()["items"][0]["revoked_at"] is not None


def test_an_api_key_cannot_revoke_another_administrators_key(client, auth_as, people):
    with Session(people) as session:
        session.add(User(unique_id="0tmnyhfztdwc1000w2kspzsnh8", username="chief", first_name="C", last_name="D", email=None, platform_role="admin"))
        session.commit()
    as_hub(auth_as, "0tmnyhfztdwc1000w2kspzsnh8", role=PlatformRole.ADMIN)
    chiefs = create(client).json()
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    refused = client.delete(f"/users/0tmnyhfztdwc1000w2kspzsnh8/apikeys/{chiefs['id']}")
    assert refused.status_code == 403 and authorize(chiefs["api_key"]).user_id == "0tmnyhfztdwc1000w2kspzsnh8"
    as_hub(auth_as, ADMIN, role=PlatformRole.ADMIN)
    assert client.delete(f"/users/0tmnyhfztdwc1000w2kspzsnh8/apikeys/{chiefs['id']}").status_code == 204


def test_key_lists_come_in_pages_newest_first(client, auth_as, people):
    now = datetime.now(timezone.utc)
    with Session(people) as session:
        for day in range(5):
            store_key(session, user_id(people, ADA), f"revoked-{day}-0000", created_at=now - timedelta(days=day)).revoked_at = now
        session.commit()
    auth_as(ADA)
    first = client.get("/account/apikeys", params={"state": "inactive", "limit": 2}).json()
    assert (first["total"], len(first["items"]), first["next_cursor"] is not None) == (5, 2, True)
    seen, cursor = [item["created_at"] for item in first["items"]], first["next_cursor"]
    while cursor:
        page = client.get("/account/apikeys", params={"state": "inactive", "limit": 2, "cursor": cursor}).json()
        seen += [item["created_at"] for item in page["items"]]
        cursor = page["next_cursor"]
    assert len(seen) == 5 and seen == sorted(seen, reverse=True)
    assert client.get("/account/apikeys", params={"state": "active", "cursor": first["next_cursor"]}).status_code == 422
    auth_as(BOB)
    assert client.get("/account/apikeys", params={"state": "inactive", "cursor": first["next_cursor"]}).status_code == 422
