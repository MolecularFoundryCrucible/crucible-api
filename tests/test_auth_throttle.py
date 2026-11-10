from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app import config
from app.auth import throttle
from app.auth.passwords import store_password
from app.auth.throttle import (
    ADDRESS_FREE_FAILURES, KNOWN_LIMIT, TOTAL_LIMIT, UNKNOWN_LIMIT, DeviceCookie, attempt_password, begin_attempt, client_address, finish_attempt,
    issue_device_cookie, read_device_cookie,
)
from app.tables import AuthEvent, DeviceAuthorization, LocalCredential, LoginThrottle, PasswordToken, User, UserIdentity

NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
RIGHT = "violet ladder under quiet rain"
WRONG = "not the right password at all"


@pytest.fixture(autouse=True)
def fast_settings(monkeypatch):
    for name, value in {"CRUCIBLE_AUTH_ARGON2_MEMORY_KIB": "19456", "CRUCIBLE_AUTH_ARGON2_TIME_COST": "2", "CRUCIBLE_AUTH_PEPPER": "p" * 40, "CRUCIBLE_AUTH_PEPPER_ID": "one"}.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()


@pytest.fixture
def engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[User.__table__, UserIdentity.__table__, LocalCredential.__table__, LoginThrottle.__table__, PasswordToken.__table__, AuthEvent.__table__])
    yield engine
    engine.dispose()


def person(engine, unique_id="0tmnyhfztdwc1000w2kspzsnh0", password=RIGHT, changed=NOW - timedelta(days=1)):
    with Session(engine) as session:
        user = User(unique_id=unique_id, first_name="A", last_name="B")
        session.add(user)
        session.commit()
        store_password(session, user, password, now=changed)
        session.commit()
        return user.id


def credential(engine, user_id):
    with Session(engine) as session:
        return session.get(LocalCredential, user_id)


def attempt(engine, user_id, password=WRONG, *, at=NOW, address=None, cookie=None, session_authenticated=False):
    return attempt_password(engine, user_id, password, address, device_cookie=cookie, session_authenticated=session_authenticated, now=at)


def cookie_for(engine, user_id):
    return read_device_cookie(issue_device_cookie(user_id, credential(engine, user_id).password_changed_at), user_id)


def fail_until_delayed(engine, user_id, *, at=NOW):
    for _ in range(throttle.UNKNOWN_FREE_FAILURES + 1):
        assert attempt(engine, user_id, at=at) is False
    return credential(engine, user_id).blocked_until


def test_right_password_passes_and_wrong_fails(engine):
    user = person(engine)
    assert attempt(engine, user, RIGHT) is True
    assert attempt(engine, user, WRONG) is False


def test_first_four_failures_no_delay_then_delay_doubles_from_2_seconds(engine):
    user = person(engine)
    for _ in range(4):
        attempt(engine, user)
    assert credential(engine, user).blocked_until is None
    attempt(engine, user)
    assert credential(engine, user).blocked_until.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=2)
    attempt(engine, user, at=NOW + timedelta(seconds=2))
    assert credential(engine, user).blocked_until.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=2 + 4)


def test_delay_caps_at_900_seconds():
    assert throttle.delay_after(4, 4) is None
    assert throttle.delay_after(5, 4) == timedelta(seconds=2)
    assert throttle.delay_after(13, 4) == timedelta(seconds=512)
    assert throttle.delay_after(14, 4) == timedelta(seconds=900)
    assert throttle.delay_after(80, 4) == timedelta(seconds=900)


def test_success_resets(engine):
    user = person(engine)
    fail_until_delayed(engine, user)
    assert attempt(engine, user, RIGHT, at=NOW + timedelta(seconds=3)) is True
    row = credential(engine, user)
    assert (row.failed_attempts, row.trusted_failed_attempts, row.blocked_until, row.locked_at) == (0, 0, None, None)


def test_attempt_during_delay_not_counted_and_correct_password_fails(engine, monkeypatch):
    user = person(engine)
    fail_until_delayed(engine, user)
    checked = []
    original = throttle.finish_attempt
    monkeypatch.setattr(throttle, "finish_attempt", lambda *args, **kwargs: checked.append(1) or original(*args, **kwargs))
    assert attempt(engine, user, RIGHT, at=NOW + timedelta(seconds=1)) is False
    assert credential(engine, user).failed_attempts == 5
    assert checked == []


def test_unknown_browsers_locked_at_90_and_correct_password_refused_then(engine):
    user = person(engine)
    at = NOW
    for _ in range(UNKNOWN_LIMIT):
        attempt(engine, user, at=at)
        at += throttle.LONGEST_DELAY
    assert credential(engine, user).failed_attempts == UNKNOWN_LIMIT
    assert attempt(engine, user, RIGHT, at=at) is False
    assert credential(engine, user).locked_at is None


def test_90_failures_take_about_20_hours_at_the_cap(engine):
    user = person(engine)
    at, spent = NOW, timedelta()
    for _ in range(UNKNOWN_LIMIT):
        attempt(engine, user, at=at)
        blocked = credential(engine, user).blocked_until
        wait = blocked.replace(tzinfo=timezone.utc) - at if blocked else timedelta()
        spent += wait
        at += wait
    assert timedelta(hours=19) < spent < timedelta(hours=21)


def test_cookie_bypasses_account_delay_and_works_while_unknown_browsers_are_locked(engine):
    user = person(engine)
    fail_until_delayed(engine, user)
    assert attempt(engine, user, RIGHT, at=NOW + timedelta(seconds=1), cookie=cookie_for(engine, user)) is True


def test_correct_password_with_cookie_clears_the_lockout(engine):
    user = person(engine)
    fail_until_delayed(engine, user)
    attempt(engine, user, RIGHT, at=NOW + timedelta(seconds=1), cookie=cookie_for(engine, user))
    assert attempt(engine, user, RIGHT, at=NOW + timedelta(seconds=1)) is True


def test_cookie_for_other_user_or_tampered_cookie_ignored(engine):
    user = person(engine)
    other = person(engine, "0tmnyhfztdwc1000w2kspzsnh1")
    value = issue_device_cookie(other, credential(engine, other).password_changed_at)
    assert read_device_cookie(value, user) is None
    assert read_device_cookie(value[:-2] + ("AA" if not value.endswith("AA") else "BB"), other) is None
    assert read_device_cookie("", user) is None and read_device_cookie("x" * 600, user) is None


def test_session_or_user_token_signature_is_not_a_device_cookie(engine):
    from itsdangerous import URLSafeTimedSerializer

    user = person(engine)
    other_salt = URLSafeTimedSerializer("test-only", salt="itsdangerous").dumps({"u": user, "p": NOW.isoformat()})
    assert read_device_cookie(other_salt, user) is None


def test_cookie_expires(engine, monkeypatch):
    user = person(engine)
    value = issue_device_cookie(user, credential(engine, user).password_changed_at)
    monkeypatch.setattr(throttle.URLSafeTimedSerializer, "loads", lambda self, data, max_age=None: (_ for _ in ()).throw(throttle.BadData("expired")))
    assert read_device_cookie(value, user) is None


def test_tenth_known_browser_failure_is_still_checked_and_then_the_password_is_disabled(engine):
    user = person(engine)
    cookie = cookie_for(engine, user)
    for _ in range(KNOWN_LIMIT - 1):
        attempt(engine, user, cookie=cookie)
    assert credential(engine, user).locked_at is None
    assert attempt(engine, user, RIGHT, cookie=cookie) is True
    for _ in range(KNOWN_LIMIT):
        attempt(engine, user, cookie=cookie)
    assert credential(engine, user).locked_at is not None
    assert attempt(engine, user, RIGHT, cookie=cookie) is False
    assert attempt(engine, user, RIGHT, session_authenticated=True) is False


def test_known_browser_has_no_delays(engine):
    user = person(engine)
    cookie = cookie_for(engine, user)
    for _ in range(KNOWN_LIMIT - 1):
        attempt(engine, user, cookie=cookie)
    assert credential(engine, user).blocked_until is None


def test_total_guesses_never_pass_100(engine):
    user = person(engine)
    cookie = cookie_for(engine, user)
    at = NOW
    for _ in range(UNKNOWN_LIMIT):
        attempt(engine, user, at=at)
        at += throttle.LONGEST_DELAY
    for _ in range(KNOWN_LIMIT + 5):
        attempt(engine, user, at=at, cookie=cookie)
    row = credential(engine, user)
    assert row.failed_attempts + row.trusted_failed_attempts == TOTAL_LIMIT and row.locked_at is not None


def test_password_change_cancels_cookies_and_a_guess_at_the_old_password_cannot_succeed(engine):
    user = person(engine)
    cookie = cookie_for(engine, user)
    decision = begin_attempt(engine, user, None, now=NOW)
    with Session(engine) as session:
        store_password(session, session.get(User, user), "an entirely new passphrase", now=NOW + timedelta(seconds=1))
        session.commit()
    assert finish_attempt(engine, decision, success=True) is False
    assert begin_attempt(engine, user, None, device_cookie=cookie, now=NOW + timedelta(seconds=2)).trusted is False


def test_each_sign_in_issues_a_new_cookie(engine):
    user = person(engine)
    changed = credential(engine, user).password_changed_at
    first = issue_device_cookie(user, changed)
    import time

    time.sleep(1.1)
    assert issue_device_cookie(user, changed) != first


def test_attackers_guesses_do_not_block_a_signed_in_password_change(engine):
    user = person(engine)
    fail_until_delayed(engine, user)
    assert attempt(engine, user, RIGHT, at=NOW + timedelta(seconds=1), session_authenticated=True) is True


def test_crash_during_hash_counts_as_a_failure(engine, monkeypatch):
    user = person(engine)

    def crash(*args, **kwargs):
        raise RuntimeError("worker died")

    monkeypatch.setattr("app.auth.passwords.verify_password", crash)
    with pytest.raises(RuntimeError):
        attempt(engine, user, RIGHT)
    assert credential(engine, user).failed_attempts == 1


def test_unknown_user_and_no_password_cost_the_same_work(engine, monkeypatch):
    dummies = []
    monkeypatch.setattr("app.auth.passwords.verify_dummy", lambda password: dummies.append(password))
    assert attempt(engine, None, WRONG) is False
    assert attempt(engine, 9999, WRONG) is False
    assert dummies == [WRONG, WRONG]


def test_address_limit_applies_across_accounts_and_counts_unknown_usernames(engine):
    users = [person(engine, f"0tmnyhfztdwc1000w2kspznh{number:02d}") for number in range(3)]
    for number in range(ADDRESS_FREE_FAILURES):
        attempt(engine, users[number % 3] if number % 2 else None, address="203.0.113.7")
    attempt(engine, users[0], address="203.0.113.7")
    assert attempt(engine, users[1], RIGHT, address="203.0.113.7") is False
    assert attempt(engine, users[1], RIGHT, address="198.51.100.1") is True


def test_an_address_is_slowed_for_at_most_a_minute(engine):
    victim = person(engine)
    for step in range(40):
        attempt(engine, None, at=NOW + timedelta(seconds=step * 61), address="10.0.0.1")
    with Session(engine) as session:
        row = session.exec(select(LoginThrottle)).one()
    assert row.consecutive_failures == 40 and row.blocked_until - row.last_failure_at <= throttle.LONGEST_ADDRESS_DELAY
    assert attempt(engine, victim, RIGHT, at=row.blocked_until + timedelta(seconds=1), address="10.0.0.1")


def test_a_known_browser_signs_in_while_its_address_is_slowed(engine):
    victim = person(engine)
    cookie = cookie_for(engine, victim)
    for _ in range(throttle.ADDRESS_FREE_FAILURES + 1):
        attempt(engine, None, address="10.0.0.1")
    assert not attempt(engine, victim, RIGHT, address="10.0.0.1")
    assert attempt(engine, victim, RIGHT, address="10.0.0.1", cookie=cookie)


def test_address_success_gives_back_only_its_own_count(engine):
    user = person(engine)
    for _ in range(ADDRESS_FREE_FAILURES - 1):
        attempt(engine, None, address="203.0.113.7")
    for _ in range(30):
        assert attempt(engine, user, RIGHT, address="203.0.113.7") is True
    with Session(engine) as session:
        assert session.exec(select(LoginThrottle.consecutive_failures)).one() == ADDRESS_FREE_FAILURES - 1


def test_address_count_restarts_after_an_hour_without_failures(engine):
    for _ in range(ADDRESS_FREE_FAILURES):
        attempt(engine, None, address="203.0.113.7")
    attempt(engine, None, address="203.0.113.7", at=NOW + timedelta(hours=1, seconds=1))
    with Session(engine) as session:
        assert session.exec(select(LoginThrottle.consecutive_failures)).one() == 1


def test_address_is_stored_only_as_a_keyed_hash(engine):
    attempt(engine, None, address="203.0.113.7")
    with Session(engine) as session:
        stored = session.exec(select(LoginThrottle.address_hash)).one()
    assert "203.0.113.7" not in stored and len(stored) == 64


def test_address_row_removed_after_30_days(engine, use_engine):
    from app.auth.cleanup import cleanup_sign_in_records

    from tests.conftest import TEST_TABLES

    attempt(engine, None, address="203.0.113.7")
    from app.tables import AuthEvent, AccessToken, LoginCode, LoginTransaction, RefreshToken, TokenFamily

    SQLModel.metadata.create_all(engine, tables=list(dict.fromkeys(TEST_TABLES + [model.__table__ for model in (AuthEvent, AccessToken, LoginCode, LoginTransaction, RefreshToken, TokenFamily, DeviceAuthorization)])))
    counts = cleanup_sign_in_records(engine, commit=True, now=NOW + timedelta(days=29))
    assert counts["login_throttle"] == 0
    counts = cleanup_sign_in_records(engine, commit=True, now=NOW + timedelta(days=31))
    assert counts["login_throttle"] == 1


def test_client_address_reads_the_address_the_server_resolved():
    class Request:
        client = type("Client", (), {"host": "192.0.2.4"})()

    assert client_address(Request()) == "192.0.2.4"
    Request.client = None
    assert client_address(Request()) is None


def test_spoofed_forwarded_header_does_not_change_address_unless_the_proxy_is_trusted():
    from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

    seen = []

    async def app(scope, receive, send):
        seen.append(scope["client"][0])

    async def run(trusted):
        scope = {"type": "http", "client": ("10.0.0.5", 1234), "headers": [(b"x-forwarded-for", b"6.6.6.6")], "scheme": "http"}
        await ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None)

    import asyncio

    asyncio.run(run("127.0.0.1"))
    asyncio.run(run("10.0.0.5"))
    assert seen == ["10.0.0.5", "6.6.6.6"]


def test_hash_slots_bound_concurrent_checks(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_HASH_CONCURRENCY", "1")
    config.get_settings.cache_clear()
    monkeypatch.setattr(throttle, "_slots", None)
    with throttle.hash_slot():
        with pytest.raises(throttle.HashBusy):
            with throttle.hash_slot(timeout=0.05):
                pass
    with throttle.hash_slot(timeout=0.05):
        pass


@pytest.mark.parametrize("days", ["0", "-1"])
def test_zero_or_negative_cookie_lifetime_fails_settings_check(monkeypatch, days):
    monkeypatch.setenv("CRUCIBLE_AUTH_DEVICE_COOKIE_DAYS", days)
    config.get_settings.cache_clear()
    with pytest.raises(config.ConfigurationError):
        config.get_settings()


def test_rehash_applied_on_success(engine, monkeypatch):
    user = person(engine)
    monkeypatch.setenv("CRUCIBLE_AUTH_ARGON2_MEMORY_KIB", "47104")
    config.get_settings.cache_clear()
    assert attempt(engine, user, RIGHT) is True
    assert credential(engine, user).password_hash.startswith("$argon2id$v=19$m=47104,")


def test_rename_keeps_lock_and_freed_username_gets_no_counters(engine):
    user = person(engine)
    fail_until_delayed(engine, user)
    with Session(engine) as session:
        row = session.get(User, user)
        row.username = "renamed"
        session.add(row)
        session.commit()
        newcomer = User(unique_id="0tmnyhfztdwc1000w2kspzsnh5", username="old-name", first_name="N", last_name="P")
        session.add(newcomer)
        session.commit()
        newcomer_id = newcomer.id
    assert credential(engine, user).failed_attempts == 5
    assert credential(engine, newcomer_id) is None


def test_device_cookie_round_trip(engine):
    user = person(engine)
    cookie = cookie_for(engine, user)
    assert cookie == DeviceCookie(user, credential(engine, user).password_changed_at.replace(tzinfo=timezone.utc))
