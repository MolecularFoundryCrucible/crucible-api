import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app.config import get_settings
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel

from app import config
from app.auth import throttle
from app.auth.passwords import store_password
from app.tables import AuthEvent, LocalCredential, LoginThrottle, PasswordToken, User, UserIdentity

RIGHT = "violet ladder under quiet rain"


@pytest.fixture(autouse=True)
def fast_settings(monkeypatch):
    for name, value in {"CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one", "CRUCIBLE_AUTH_HASH_CONCURRENCY": "8"}.items():
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
    schema = "throttle_test_" + uuid.uuid4().hex
    admin = create_engine(value)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(value, pool_size=25, max_overflow=5, connect_args={"options": f"-csearch_path={schema}"})
    try:
        SQLModel.metadata.create_all(engine, tables=[User.__table__, UserIdentity.__table__, LocalCredential.__table__, LoginThrottle.__table__, PasswordToken.__table__, AuthEvent.__table__])
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def person(engine) -> int:
    with Session(engine) as session:
        user = User(unique_id="0tmnyhfztdwc1000w2kspzsnh0", first_name="A", last_name="B")
        session.add(user)
        session.commit()
        store_password(session, user, RIGHT, now=datetime(2026, 11, 1, tzinfo=timezone.utc))
        session.commit()
        return user.id


def test_parallel_guesses_cannot_exceed_the_counter(pg_engine):
    user = person(pg_engine)
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=20) as pool:
        decisions = list(pool.map(lambda _: throttle.begin_attempt(pg_engine, user, "203.0.113.7", now=now), range(20)))
    with Session(pg_engine) as session:
        credential = session.get(LocalCredential, user)
        address = session.get(LoginThrottle, throttle.address_hash("203.0.113.7"))
    checked = sum(decision.check_password for decision in decisions)
    assert checked == credential.failed_attempts == throttle.UNKNOWN_FREE_FAILURES + 1
    assert address.consecutive_failures == get_settings().auth.address_free_failures + 1


def test_parallel_known_browser_guesses_stop_exactly_at_the_limit(pg_engine):
    user = person(pg_engine)
    with Session(pg_engine) as session:
        changed = session.get(LocalCredential, user).password_changed_at
    cookie = throttle.read_device_cookie(throttle.issue_device_cookie(user, changed), user)
    with ThreadPoolExecutor(max_workers=20) as pool:
        decisions = list(pool.map(lambda _: throttle.begin_attempt(pg_engine, user, None, device_cookie=cookie), range(20)))
    with Session(pg_engine) as session:
        credential = session.get(LocalCredential, user)
    assert sum(decision.check_password for decision in decisions) == credential.trusted_failed_attempts == throttle.KNOWN_LIMIT
    assert credential.locked_at is not None


def test_no_connection_held_during_hash(pg_engine, monkeypatch):
    user = person(pg_engine)
    held = []
    from app.auth import passwords

    original = passwords.verify_password

    def watching(*args, **kwargs):
        held.append(pg_engine.pool.checkedout())
        return original(*args, **kwargs)

    monkeypatch.setattr(passwords, "verify_password", watching)
    assert throttle.attempt_password(pg_engine, user, RIGHT, "203.0.113.7") is True
    assert held == [0]
