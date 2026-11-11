import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app.tables import ApiKey, ApiToken, User
from app.utils import auth_utils
from app.utils.permissions import PlatformRole

ADA = "0tmnyhfztdwc1000w2kspzsnh0"
ADMIN = "0tmnyhfztdwc1000w2kspzsnh9"


@pytest.fixture
def ada(db_engine, use_engine):
    use_engine(db_engine)
    with Session(db_engine) as session:
        user = User(unique_id=ADA, username="ada", first_name="A", last_name="L", email=None)
        session.add_all([user, User(unique_id=ADMIN, username="boss", first_name="B", last_name="O", email=None, platform_role="admin")])
        session.commit()
        return user.id


def keys(engine):
    with Session(engine) as session:
        return session.exec(select(ApiKey).order_by(ApiKey.id)).all()


def tokens(engine):
    with Session(engine) as session:
        return session.exec(select(ApiToken.token)).all()


def test_new_key_is_written_to_both_tables_as_a_hash(db_engine, ada):
    key = auth_utils.find_or_add_api_access(ada, db_engine)
    [row] = keys(db_engine)
    assert tokens(db_engine) == [key]
    assert (row.user_id, row.key_hash, row.key_last4) == (ada, hashlib.sha256(key.encode()).hexdigest(), key[-4:])
    assert row.revoked_at is None and row.expires_at.replace(tzinfo=timezone.utc) - row.created_at.replace(tzinfo=timezone.utc) == timedelta(days=365)
    assert key not in [value for value in row.model_dump().values() if isinstance(value, str)]


def test_regenerated_key_is_gone_from_both_tables(db_engine, ada):
    old = auth_utils.find_or_add_api_access(ada, db_engine)
    new = auth_utils.find_or_add_api_access(ada, db_engine, regenerate=True)
    assert tokens(db_engine) == [new]
    assert [(row.key_last4, row.revoked_at is not None) for row in keys(db_engine)] == [(old[-4:], True), (new[-4:], False)]


def test_revoked_key_is_gone_from_both_tables(client, auth_as, db_engine, ada):
    auth_utils.find_or_add_api_access(ada, db_engine)
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert client.delete(f"/users/{ADA}/apikey").status_code == 204
    assert tokens(db_engine) == []
    assert [row.revoked_at is not None for row in keys(db_engine)] == [True]


def test_rejected_waiting_account_takes_its_hashed_keys_with_it(db_engine, ada):
    from app.auth.api_keys import store_key
    from app.auth.registration import reject_pending_user

    with Session(db_engine) as session:
        user = session.get(User, ada)
        user.pending_at = datetime.now(timezone.utc)
        store_key(session, ada, "e" * 64)
        session.commit()
        reject_pending_user(session, session.get(User, ada), actor=None)
        session.commit()
    assert keys(db_engine) == []


def test_a_service_account_key_authenticates_through_its_hash(client, auth_as, db_engine, ada):
    from fastapi.security import HTTPAuthorizationCredentials

    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as("0000-0000-0000-0042", platform_role=PlatformRole.ADMIN)
    created = client.post("/service_accounts", json={"username": "robot"})
    assert created.status_code == 200, created.text
    key = created.json()["api_key"]
    with Session(db_engine) as session:
        assert session.exec(select(ApiKey).where(ApiKey.key_hash == hashlib.sha256(key.encode()).hexdigest())).one() is not None
    assert auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)).is_service_account


def test_an_invalid_key_is_401_and_never_a_redirect(client, db_engine, ada):
    from app.api.main import app
    from app.utils.auth_utils import authenticate_user

    app.dependency_overrides.pop(authenticate_user, None)
    for path in ("/account/profile", "/datasets", "/users"):
        answer = client.get(path, headers={"Authorization": "Bearer not-a-real-key"}, follow_redirects=False)
        assert answer.status_code == 401 and "location" not in answer.headers, (path, answer.status_code)
