import importlib.util
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, inspect, text
from sqlmodel import Session, SQLModel, select

import app.utils.auth_utils as auth_utils
from app.auth.api_keys import newest_key, status_of
from app.models.core_models import ApiToken, HubAccessToken, HubLoginCode, HubRefreshToken, HubTokenFamily, User
from app.utils.hub_tokens import hub_access_family, issue_hub_tokens, refresh_hub_tokens, revoke_all_hub_tokens

ORCID = "0000-0002-1825-0097"
MFID = "0tmnyhfztdwc1000w2kspzsnh9"

pytestmark = pytest.mark.usefixtures("hub_client_registered")


@pytest.fixture
def person_with_key_and_hub_session(db_engine, use_engine):
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginCode, HubTokenFamily, HubRefreshToken, HubAccessToken)])
    use_engine(db_engine)
    with auth_utils._auth_cache_lock:
        auth_utils._auth_cache.clear()
    with Session(db_engine) as session, session.begin():
        user = User(unique_id=ORCID, first_name="Ada", last_name="Lovelace", email=None, username="ada")
        session.add(user)
        session.flush()
        user_id = user.id
    key = auth_utils.find_or_add_api_access(user_id, db_engine)
    pair = issue_hub_tokens(db_engine, user_id, "crucible-hub")
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
        assert [(t.user_id, t.user_unique_id) for t in session.exec(select(ApiToken)).all()] == [(user_id, ORCID)]
        assert [(f.user_id, f.user_unique_id) for f in session.exec(select(HubTokenFamily)).all()] == [(user_id, ORCID)]


def test_key_and_hub_session_still_work_after_unique_id_changes(db_engine, person_with_key_and_hub_session):
    user_id, key, pair = person_with_key_and_hub_session
    assert (authorize(key).user_id, authorize(key).internal_user_id) == (MFID, user_id)
    assert authorize(pair.access_token).user_id == MFID
    assert refresh_hub_tokens(db_engine, pair.refresh_token, "crucible-hub").user_unique_id == MFID
    with Session(db_engine) as session:
        assert status_of(newest_key(session, user_id)) == "active"
    assert auth_utils.find_or_add_api_access(user_id, db_engine) == key


def test_revoking_hub_sessions_by_internal_id_after_unique_id_changes(db_engine, person_with_key_and_hub_session):
    user_id, _, pair = person_with_key_and_hub_session
    with Session(db_engine) as session, session.begin():
        revoke_all_hub_tokens(session, user_id, now=datetime.now(timezone.utc))
    with Session(db_engine) as session:
        with pytest.raises(Exception):
            hub_access_family(session, pair.access_token)


def test_a_key_with_no_owner_signs_nobody_in(db_engine, use_engine):
    use_engine(db_engine)
    with Session(db_engine) as session, session.begin():
        session.add(ApiToken(token="orphan", access_group_id="g", user_unique_id="nobody", token_created=datetime.now(timezone.utc).date().isoformat()))
    with pytest.raises(Exception) as caught:
        authorize("orphan")
    assert caught.value.status_code == 401


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_token_user_id_migration_fills_rows_and_old_style_writes():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261105_7b4e9d2a1c60_token_user_id.py"
    spec = importlib.util.spec_from_file_location("token_user_id_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_token_user_id_" + uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            run = lambda sql: connection.execute(text(sql))  # noqa: E731
            run(f'CREATE SCHEMA "{schema}"')
            run(f'SET LOCAL search_path TO "{schema}"')
            run('CREATE TABLE "user" (id serial PRIMARY KEY, orcid text UNIQUE)')
            run('CREATE TABLE apitoken (access_group_id text NOT NULL, token text NOT NULL, user_unique_id text REFERENCES "user"(orcid), PRIMARY KEY (access_group_id, token))')
            run('CREATE TABLE hubtokenfamily (id text PRIMARY KEY, user_unique_id text NOT NULL REFERENCES "user"(orcid))')
            run('CREATE TABLE hublogincode (code_hash text PRIMARY KEY, user_unique_id text NOT NULL REFERENCES "user"(orcid))')
            run(f"INSERT INTO \"user\" (orcid) VALUES ('{ORCID}'), ('{MFID}')")
            run(f"INSERT INTO apitoken VALUES ('g', 'existing', '{ORCID}'), ('g', 'unowned', NULL)")
            run(f"INSERT INTO hubtokenfamily VALUES ('existing', '{ORCID}')")
            run(f"INSERT INTO hublogincode VALUES ('existing', '{ORCID}')")
            ada, other = connection.execute(text('SELECT id FROM "user" ORDER BY id')).scalars().all()
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                assert dict(connection.execute(text("SELECT token, user_id FROM apitoken")).all()) == {"existing": ada, "unowned": None}
                run(f"INSERT INTO apitoken (access_group_id, token, user_unique_id) VALUES ('g', 'old-pod', '{MFID}')")
                run(f"INSERT INTO hubtokenfamily (id, user_unique_id) VALUES ('old-pod', '{MFID}')")
                run(f"INSERT INTO hublogincode (code_hash, user_unique_id) VALUES ('old-pod', '{MFID}')")
                run(f"INSERT INTO apitoken (access_group_id, token, user_unique_id, user_id) VALUES ('g', 'new-pod', '{ORCID}', {ada})")
                for table in migration.TABLES:
                    key = "token" if table == "apitoken" else "id" if table == "hubtokenfamily" else "code_hash"
                    rows = dict(connection.execute(text(f"SELECT {key}, user_id FROM {table}")).all())
                    assert rows["existing"] == ada and rows["old-pod"] == other
                assert connection.execute(text("SELECT user_id FROM apitoken WHERE token = 'new-pod'")).scalar() == ada
                migration.downgrade()
                for table in migration.TABLES:
                    assert "user_id" not in {c["name"] for c in inspect(connection).get_columns(table, schema=schema)}
                assert connection.execute(text("SELECT count(*) FROM pg_proc WHERE proname = 'fill_user_id_from_unique_id'")).scalar() == 0
        finally:
            transaction.rollback()
    engine.dispose()
