import importlib.util
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, select

from app import config
from app.auth import throttle
from app.auth.password_links import LinkRefused, issue_link, use_link
from app.tables import (
    DeviceAuthorization, HubAccessToken, HubLoginCode, HubRefreshToken, HubTokenFamily, LocalCredential, PasswordToken, User, UserIdentity, AuthEvent,
)

NEW = "violet ladder under quiet rain"


@pytest.fixture(autouse=True)
def fast_settings(monkeypatch):
    for name, value in {"CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one"}.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    monkeypatch.setattr(throttle, "_slots", None)


@pytest.fixture
def pg_engine():
    value = os.getenv("HUB_AUTH_TEST_DATABASE_URL")
    if not value:
        pytest.skip("HUB_AUTH_TEST_DATABASE_URL requires an isolated local PostgreSQL database")
    url = make_url(value)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "crucible_hub_auth_test":
        pytest.fail("Use a local database named crucible_hub_auth_test")
    schema = "password_links_test_" + uuid.uuid4().hex
    admin = create_engine(value)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(value, pool_size=10, connect_args={"options": f"-csearch_path={schema}"})
    try:
        SQLModel.metadata.create_all(engine, tables=[model.__table__ for model in (
            User, UserIdentity, AuthEvent, LocalCredential, PasswordToken, HubTokenFamily, HubRefreshToken, HubAccessToken, HubLoginCode, DeviceAuthorization,
        )])
        with Session(engine) as session:
            session.add(User(unique_id="0tmnyhfztdwc1000w2kspzsnh0", username="ada", first_name="A", last_name="B"))
            session.commit()
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def ada(session):
    return session.exec(select(User)).one()


def test_two_tabs_cannot_both_use_a_link(pg_engine):
    with Session(pg_engine) as session, session.begin():
        token, _ = issue_link(session, ada(session), "setup", actor=None)
    barrier = threading.Barrier(2)

    def use(password):
        barrier.wait(timeout=10)
        try:
            with Session(pg_engine) as session, session.begin():
                use_link(session, token, password)
            return True
        except LinkRefused:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(use, (NEW, NEW + " too")))
    assert sorted(results) == [False, True]


def test_parallel_issues_leave_one_live_link(pg_engine):
    barrier = threading.Barrier(4)

    def issue(_):
        barrier.wait(timeout=10)
        with Session(pg_engine) as session, session.begin():
            return issue_link(session, ada(session), "setup", actor=None)[0]

    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(issue, range(4)))
    assert len(set(tokens)) == 4
    with Session(pg_engine) as session:
        live = session.exec(select(PasswordToken).where(PasswordToken.consumed_at == None, PasswordToken.voided_at == None)).all()  # noqa: E711
    assert len(live) == 1


def test_the_index_refuses_a_second_live_link(pg_engine):
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    with Session(pg_engine) as session:
        user_id = ada(session).id
        for digest in ("a" * 64, "b" * 64):
            session.add(PasswordToken(token_hash=digest, user_id=user_id, purpose="reset", created_at=now, expires_at=now + timedelta(hours=1)))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.skipif(not os.getenv("CRUCIBLE_TEST_PG_URL"), reason="requires isolated test PostgreSQL")
def test_password_token_migration_roundtrip():
    path = Path(__file__).resolve().parents[1] / "migrations/versions/20261108_a7df9dd039cf_password_token.py"
    spec = importlib.util.spec_from_file_location("password_token_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(os.environ["CRUCIBLE_TEST_PG_URL"])
    schema = "test_password_token_" + uuid.uuid4().hex
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            connection.execute(text('CREATE TABLE "user" (id integer PRIMARY KEY)'))
            connection.execute(text('INSERT INTO "user" (id) VALUES (1)'))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
                insert = "INSERT INTO password_token (token_hash, user_id, purpose, created_at, expires_at, voided_at) VALUES (:h, 1, 'setup', now(), now(), :v)"
                connection.execute(text(insert), {"h": "a", "v": None})
                connection.execute(text(insert), {"h": "b", "v": "2026-11-08"})
                nested = connection.begin_nested()
                with pytest.raises(IntegrityError):
                    connection.execute(text(insert), {"h": "c", "v": None})
                nested.rollback()
                migration.downgrade()
                assert "password_token" not in inspect(connection).get_table_names(schema=schema)
        finally:
            transaction.rollback()
    engine.dispose()
