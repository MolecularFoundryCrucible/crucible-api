from datetime import datetime, timezone

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session, SQLModel, select

import app.utils.auth_utils as auth_utils
from app.auth.api_keys import newest_key, status_of
from app.auth.api_keys import create_key
from app.tables import ApiKey, AccessToken, LoginCode, RefreshToken, TokenFamily, User
from app.auth.tokens import access_token_family, issue_tokens, refresh_tokens, revoke_all_tokens

ORCID = "0000-0002-1825-0097"
MFID = "0tmnyhfztdwc1000w2kspzsnh9"

pytestmark = pytest.mark.usefixtures("hub_client_registered")


@pytest.fixture
def person_with_key_and_hub_session(db_engine, use_engine):
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (LoginCode, TokenFamily, RefreshToken, AccessToken)])
    use_engine(db_engine)
    with auth_utils._auth_cache_lock:
        auth_utils._auth_cache.clear()
    with Session(db_engine) as session, session.begin():
        user = User(unique_id=ORCID, first_name="Ada", last_name="Lovelace", email=None, username="ada")
        session.add(user)
        session.flush()
        user_id = user.id
        key = create_key(session, user)[1]
    pair = issue_tokens(db_engine, user_id, "crucible-hub")
    with Session(db_engine) as session, session.begin():
        session.get(User, user_id).unique_id = MFID
    with auth_utils._auth_cache_lock:
        auth_utils._auth_cache.clear()
    return user_id, key, pair


def authorize(token):
    return auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))


def test_new_keys_and_hub_sessions_name_the_person_by_internal_id(db_engine, person_with_key_and_hub_session):
    user_id, _, _ = person_with_key_and_hub_session
    with Session(db_engine) as session:
        assert [k.user_id for k in session.exec(select(ApiKey)).all()] == [user_id]
        assert [(f.user_id, f.user_unique_id) for f in session.exec(select(TokenFamily)).all()] == [(user_id, ORCID)]


def test_key_and_hub_session_still_work_after_unique_id_changes(db_engine, person_with_key_and_hub_session):
    user_id, key, pair = person_with_key_and_hub_session
    assert (authorize(key).user_id, authorize(key).internal_user_id) == (MFID, user_id)
    assert authorize(pair.access_token).user_id == MFID
    assert refresh_tokens(db_engine, pair.refresh_token, "crucible-hub").user_unique_id == MFID
    with Session(db_engine) as session:
        assert status_of(newest_key(session, user_id)) == "active"


def test_revoking_hub_sessions_by_internal_id_after_unique_id_changes(db_engine, person_with_key_and_hub_session):
    user_id, _, pair = person_with_key_and_hub_session
    with Session(db_engine) as session, session.begin():
        revoke_all_tokens(session, user_id, now=datetime.now(timezone.utc))
    with Session(db_engine) as session:
        with pytest.raises(Exception):
            access_token_family(session, pair.access_token)
