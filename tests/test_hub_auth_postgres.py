import base64
import hashlib
import importlib.util
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, create_engine, select

from app.models.core_models import DeviceAuthorization, LoginThrottle, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, User
from app.utils.hub_login import exchange_handoff_code
from app.utils.hub_tokens import InvalidHubGrant, hub_access_user, issue_hub_tokens, refresh_hub_tokens, revoke_hub_tokens
from tests.people import internal_id
from app.models.core_models import AuthEvent, UserIdentity

pytestmark = pytest.mark.usefixtures("hub_client_registered")


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
            User.__table__, UserIdentity.__table__, AuthEvent.__table__, LoginThrottle.__table__, DeviceAuthorization.__table__, HubTokenFamily.__table__, HubRefreshToken.__table__,
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
            code_hash=code_hash, user_id=internal_id(session, USER), user_unique_id=USER, client_id="crucible-hub", callback_uri=callback,
            code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode(),
            created_at=NOW, expires_at=NOW + timedelta(minutes=1),
        ))

    def fail(*args):
        raise RuntimeError("simulated issuance failure")

    with monkeypatch.context() as patch:
        patch.setattr("app.utils.hub_tokens._issue_access", fail)
        with pytest.raises(RuntimeError, match="simulated"):
            exchange_handoff_code(pg_engine, code, verifier, "crucible-hub", callback, now=NOW)
    with Session(pg_engine) as session:
        assert session.get(HubLoginCode, code_hash).consumed_at is None
        assert session.exec(select(HubTokenFamily)).all() == []
    pair = exchange_handoff_code(pg_engine, code, verifier, "crucible-hub", callback, now=NOW)
    with Session(pg_engine) as session:
        assert hub_access_user(session, pair.access_token, now=NOW) == internal_id(session, USER)
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(pg_engine, code, verifier, "crucible-hub", callback, now=NOW)


def test_concurrent_refresh_keeps_the_session_and_a_later_replay_ends_it(pg_engine):
    first = issue_hub_tokens(pg_engine, internal_id(pg_engine, USER), "crucible-hub", now=NOW)
    barrier = threading.Barrier(2)
    waited = threading.local()

    def after_read(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT") and "FROM hubrefreshtoken" in statement and "FOR UPDATE" not in statement and not getattr(waited, "done", False):
            waited.done = True
            barrier.wait(timeout=10)

    def refresh():
        try:
            return refresh_hub_tokens(pg_engine, first.refresh_token, "crucible-hub", now=NOW + timedelta(minutes=1))
        except InvalidHubGrant:
            return None

    event.listen(pg_engine, "after_cursor_execute", after_read)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: refresh(), range(2)))
    finally:
        event.remove(pg_engine, "after_cursor_execute", after_read)
    issued = [result for result in results if result is not None]
    assert len(issued) == 2
    with Session(pg_engine) as session:
        assert hub_access_user(session, issued[0].access_token, now=NOW + timedelta(minutes=1)) == hub_access_user(session, issued[1].access_token, now=NOW + timedelta(minutes=1))
    refresh_hub_tokens(pg_engine, issued[0].refresh_token, "crucible-hub", now=NOW + timedelta(minutes=2))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(pg_engine, issued[1].refresh_token, "crucible-hub", now=NOW + timedelta(minutes=3))
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(pg_engine, first.refresh_token, "crucible-hub", now=NOW + timedelta(minutes=3))


@pytest.mark.parametrize("first_operation", ["refresh", "revoke"])
def test_logout_and_refresh_serialize_without_restoring_session(pg_engine, first_operation):
    pair = issue_hub_tokens(pg_engine, internal_id(pg_engine, USER), "crucible-hub", now=NOW)
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
                return revoke_hub_tokens(pg_engine, pair.refresh_token, "crucible-hub", now=NOW)
            try:
                return refresh_hub_tokens(pg_engine, pair.refresh_token, "crucible-hub", now=NOW)
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
            refresh_hub_tokens(pg_engine, tokens.refresh_token, "crucible-hub", now=NOW)


def test_cleanup_respects_foreign_keys_and_preserves_live_family(pg_engine):
    from app.utils.hub_auth_maintenance import cleanup_hub_auth

    expired = issue_hub_tokens(pg_engine, internal_id(pg_engine, USER), "crucible-hub", now=NOW - timedelta(days=31))
    live = issue_hub_tokens(pg_engine, internal_id(pg_engine, USER), "crucible-hub", now=NOW)
    preview = cleanup_hub_auth(pg_engine, now=NOW, batch_size=1)
    assert preview["hubtokenfamily"] == 1
    counts = cleanup_hub_auth(pg_engine, commit=True, now=NOW, batch_size=1)
    assert counts == preview
    with Session(pg_engine) as session:
        assert hub_access_user(session, live.access_token, now=NOW) == internal_id(session, USER)
        assert len(session.exec(select(HubTokenFamily)).all()) == 1
        assert len(session.exec(select(HubRefreshToken)).all()) == 1
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(pg_engine, expired.refresh_token, "crucible-hub", now=NOW)


def test_expiry_index_migration_round_trip(pg_engine):
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20260928_e92a6b1c730f_index_hub_auth_expiry.py"
    spec = importlib.util.spec_from_file_location("hub_expiry_revision", path)
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    pair = issue_hub_tokens(pg_engine, internal_id(pg_engine, USER), "crucible-hub", now=NOW)

    with pg_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            revision.downgrade()
            for table, _ in revision.INDEXES:
                assert f"ix_{table}_expiry" not in {index["name"] for index in inspect(connection).get_indexes(table)}
            connection.commit()
            revision.upgrade()
            for table, key in revision.INDEXES:
                indexes = {index["name"]: index for index in inspect(connection).get_indexes(table)}
                name = f"ix_{table}_expiry"
                assert indexes[name]["column_names"] == ["expires_at", key]
                assert connection.execute(text(
                    "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
                ), {"name": name}).scalar_one()
                model_index = next(index for index in SQLModel.metadata.tables[table].indexes if index.name == name)
                assert list(model_index.columns.keys()) == ["expires_at", key]
    with Session(pg_engine) as session:
        assert hub_access_user(session, pair.access_token, now=NOW) == internal_id(session, USER)
