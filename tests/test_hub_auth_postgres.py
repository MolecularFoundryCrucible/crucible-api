import base64
import hashlib
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.core_models import HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, User
from app.utils.hub_login import exchange_handoff_code
from app.utils.hub_tokens import InvalidHubGrant, hub_access_user, issue_hub_tokens, refresh_hub_tokens, revoke_hub_tokens


NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)
USER = "0000-0000-0000-0001"


@pytest.fixture
def pg_engine():
    value = os.getenv("HUB_AUTH_TEST_DATABASE_URL")
    if not value:
        pytest.skip("HUB_AUTH_TEST_DATABASE_URL requires an isolated local PostgreSQL database")
    url = make_url(value)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "crucible_hub_auth_test":
        pytest.fail("Use a local database named crucible_hub_auth_test")
    schema = "hub_auth_test_" + uuid.uuid4().hex
    admin = create_engine(value)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(value, connect_args={"options": f"-csearch_path={schema}"})
    try:
        SQLModel.metadata.create_all(engine, tables=[
            User.__table__, HubTokenFamily.__table__, HubRefreshToken.__table__,
            HubAccessToken.__table__, HubLoginCode.__table__, HubLoginTransaction.__table__,
        ])
        with Session(engine) as session, session.begin():
            session.add(User(unique_id=USER, first_name="Test", last_name="Human"))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_exchange_rolls_back_code_and_family_on_issuance_failure(pg_engine, monkeypatch):
    verifier = "v" * 43
    code = "c" * 64
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    callback = "https://example.test/hub/auth/callback"
    with Session(pg_engine) as session, session.begin():
        session.add(HubLoginCode(
            code_hash=code_hash, user_unique_id=USER, client_id="hub", callback_uri=callback,
            code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
            created_at=NOW, expires_at=NOW + timedelta(minutes=1),
        ))

    def fail(*args):
        raise RuntimeError("simulated issuance failure")

    with monkeypatch.context() as patch:
        patch.setattr("app.utils.hub_tokens._issue_access", fail)
        with pytest.raises(RuntimeError, match="simulated"):
            exchange_handoff_code(pg_engine, code, verifier, "hub", callback, now=NOW)
    with Session(pg_engine) as session:
        assert session.get(HubLoginCode, code_hash).consumed_at is None
        assert session.exec(select(HubTokenFamily)).all() == []
    pair = exchange_handoff_code(pg_engine, code, verifier, "hub", callback, now=NOW)
    with Session(pg_engine) as session:
        assert hub_access_user(session, pair.access_token, now=NOW) == USER
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(pg_engine, code, verifier, "hub", callback, now=NOW)


def test_concurrent_refresh_detects_replay_and_revokes_family(pg_engine):
    first = issue_hub_tokens(pg_engine, USER, "hub", now=NOW)
    barrier = threading.Barrier(2)

    def after_read(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM hubrefreshtoken" in statement and "FOR UPDATE" not in statement:
            barrier.wait(timeout=10)

    def refresh():
        try:
            return refresh_hub_tokens(pg_engine, first.refresh_token, "hub", now=NOW + timedelta(minutes=1))
        except InvalidHubGrant:
            return None

    event.listen(pg_engine, "after_cursor_execute", after_read)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: refresh(), range(2)))
    finally:
        event.remove(pg_engine, "after_cursor_execute", after_read)
    issued = [result for result in results if result is not None]
    assert len(issued) == 1
    with Session(pg_engine) as session:
        with pytest.raises(InvalidHubGrant):
            hub_access_user(session, issued[0].access_token, now=NOW + timedelta(minutes=1))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(pg_engine, issued[0].refresh_token, "hub", now=NOW + timedelta(minutes=2))


@pytest.mark.parametrize("first_operation", ["refresh", "revoke"])
def test_logout_and_refresh_serialize_without_restoring_session(pg_engine, first_operation):
    pair = issue_hub_tokens(pg_engine, USER, "hub", now=NOW)
    both_read = threading.Barrier(2)
    first_finished = threading.Event()
    operation = threading.local()

    def after_read(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM hubrefreshtoken" in statement and "FOR UPDATE" not in statement:
            both_read.wait(timeout=10)

    def before_lock(connection, cursor, statement, parameters, context, executemany):
        if "FROM hubtokenfamily" in statement and "FOR UPDATE" in statement and operation.name != first_operation:
            assert first_finished.wait(timeout=10)

    def run(name):
        operation.name = name
        try:
            if name == "revoke":
                return revoke_hub_tokens(pg_engine, pair.refresh_token, "hub", now=NOW)
            try:
                return refresh_hub_tokens(pg_engine, pair.refresh_token, "hub", now=NOW)
            except InvalidHubGrant:
                return None
        finally:
            if name == first_operation:
                first_finished.set()

    event.listen(pg_engine, "after_cursor_execute", after_read)
    event.listen(pg_engine, "before_cursor_execute", before_lock)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            refreshed, _ = list(pool.map(run, ["refresh", "revoke"]))
    finally:
        event.remove(pg_engine, "after_cursor_execute", after_read)
        event.remove(pg_engine, "before_cursor_execute", before_lock)
    assert (refreshed is not None) == (first_operation == "refresh")
    for tokens in [pair] + ([refreshed] if refreshed else []):
        with Session(pg_engine) as session, pytest.raises(InvalidHubGrant):
            hub_access_user(session, tokens.access_token, now=NOW)
        with pytest.raises(InvalidHubGrant):
            refresh_hub_tokens(pg_engine, tokens.refresh_token, "hub", now=NOW)


def test_cleanup_respects_foreign_keys_and_preserves_live_family(pg_engine):
    from app.utils.hub_auth_maintenance import cleanup_hub_auth

    expired = issue_hub_tokens(pg_engine, USER, "hub", now=NOW - timedelta(days=31))
    live = issue_hub_tokens(pg_engine, USER, "hub", now=NOW)
    preview = cleanup_hub_auth(pg_engine, now=NOW, batch_size=1)
    assert preview["hubtokenfamily"] == 1
    counts = cleanup_hub_auth(pg_engine, commit=True, now=NOW, batch_size=1)
    assert counts == preview
    with Session(pg_engine) as session:
        assert hub_access_user(session, live.access_token, now=NOW) == USER
        assert len(session.exec(select(HubTokenFamily)).all()) == 1
        assert len(session.exec(select(HubRefreshToken)).all()) == 1
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(pg_engine, expired.refresh_token, "hub", now=NOW)
