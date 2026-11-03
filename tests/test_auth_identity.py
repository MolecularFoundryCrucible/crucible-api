import importlib.util
import os
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlmodel import Session, SQLModel, select

from app.api.main import app
from app.api.routes import users as users_routes
from app.auth.identity import IdentityTaken, ensure_identity
from app.models.core_models import User, UserIdentity
from app.utils.auth_utils import encode_token
from app.utils.permissions import PlatformRole

ORCID = "0000-0002-1825-0097"
OTHER_ORCID = "0000-0001-5109-3700"
ISSUER = "https://orcid.org"
SANDBOX = "https://sandbox.orcid.org"
MFID = "0vhd8h3s5x7z9b1c3e5g7j9m1p"


def add_user(session, unique_id=ORCID, **fields):
    user = User(unique_id=unique_id, first_name="Ada", last_name="Lovelace", email=None, **fields)
    session.add(user)
    session.flush()
    return user


def identities(engine):
    with Session(engine) as session:
        return [(row.issuer, row.subject) for row in session.exec(select(UserIdentity).order_by(UserIdentity.id)).all()]


def test_ensure_identity_writes_the_row(db_engine):
    with Session(db_engine) as session, session.begin():
        user = add_user(session)
        row = ensure_identity(session, user, ISSUER, ORCID)
        assert (row.user_id, row.issuer, row.subject) == (user.id, ISSUER, ORCID)
        assert row.created_at is not None and row.created_by_user_id is None


def test_ensure_identity_is_idempotent(db_engine):
    with Session(db_engine) as session, session.begin():
        user = add_user(session)
        first = ensure_identity(session, user, ISSUER, ORCID)
        assert ensure_identity(session, user, ISSUER, ORCID).id == first.id
    assert identities(db_engine) == [(ISSUER, ORCID)]


def test_ensure_identity_raises_when_held_by_another_user(db_engine):
    with Session(db_engine) as session, session.begin():
        ensure_identity(session, add_user(session), ISSUER, ORCID)
        other = add_user(session, unique_id=MFID)
        with pytest.raises(IdentityTaken):
            ensure_identity(session, other, ISSUER, ORCID)


def test_ensure_identity_records_who_made_the_link_and_the_label(db_engine):
    with Session(db_engine) as session, session.begin():
        admin = add_user(session, unique_id=MFID)
        row = ensure_identity(session, add_user(session), ISSUER, ORCID, label="ada", created_by=admin)
        assert row.created_by_user_id == admin.id and row.label == "ada"


def test_a_failed_identity_write_leaves_the_outer_transaction_usable(db_engine):
    with Session(db_engine) as session, session.begin():
        user = add_user(session)
        ensure_identity(session, user, ISSUER, ORCID)
        other = add_user(session, unique_id=MFID)
        with pytest.raises(IdentityTaken):
            ensure_identity(session, other, ISSUER, ORCID)
        other.first_name = "Grace"
    with Session(db_engine) as session:
        assert session.exec(select(User).where(User.unique_id == MFID)).one().first_name == "Grace"


def test_same_subject_at_another_issuer_is_a_different_identity(db_engine):
    with Session(db_engine) as session, session.begin():
        ensure_identity(session, add_user(session), "https://a.example", "same-sub")
        ensure_identity(session, add_user(session, unique_id=MFID), "https://b.example", "same-sub")
    assert len(identities(db_engine)) == 2


def test_several_identities_at_a_non_orcid_issuer_are_allowed(db_engine):
    with Session(db_engine) as session, session.begin():
        user = add_user(session, unique_id=MFID)
        ensure_identity(session, user, "https://cilogon.example", "one")
        ensure_identity(session, user, "https://cilogon.example", "two")
    assert len(identities(db_engine)) == 2


def test_issuer_and_subject_are_unique_in_the_database(db_engine):
    with Session(db_engine) as session:
        first, second = add_user(session), add_user(session, unique_id=MFID)
        session.add(UserIdentity(user_id=first.id, issuer=ISSUER, subject=ORCID))
        session.flush()
        session.add(UserIdentity(user_id=second.id, issuer=ISSUER, subject=ORCID))
        with pytest.raises(IntegrityError):
            session.flush()


def test_one_person_cannot_hold_two_orcids_in_the_database(db_engine):
    with Session(db_engine) as session:
        user = add_user(session, unique_id=MFID)
        session.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=ORCID))
        session.flush()
        session.add(UserIdentity(user_id=user.id, issuer=SANDBOX, subject=OTHER_ORCID))
        with pytest.raises(IntegrityError):
            session.flush()


@pytest.fixture
def hub_tables(db_engine):
    from app.models.core_models import HubLoginCode, HubLoginTransaction

    SQLModel.metadata.create_all(db_engine, tables=[HubLoginCode.__table__, HubLoginTransaction.__table__])


def test_hub_sign_in_writes_the_identity_row_for_new_and_existing_users(db_engine, hub_tables):
    from datetime import datetime, timedelta, timezone

    from app.models.core_models import HubLoginTransaction
    from app.utils.hub_login import create_handoff_code

    now = datetime.now(timezone.utc)
    def transaction(state):
        return HubLoginTransaction(state_hash=state, browser_secret_hash="b", client_id="crucible-hub", callback_uri="https://hub.example/cb", hub_state="h" * 16, code_challenge="c" * 43, nonce="n", created_at=now, expires_at=now + timedelta(minutes=10))
    create_handoff_code(db_engine, transaction("s1"), ORCID, "Ada", "Lovelace")
    create_handoff_code(db_engine, transaction("s2"), ORCID, "Ada", "Lovelace")
    with Session(db_engine) as session, session.begin():
        add_user(session, unique_id=OTHER_ORCID)
    create_handoff_code(db_engine, transaction("s3"), OTHER_ORCID, "Grace", "Hopper")
    assert identities(db_engine) == [(ISSUER, ORCID), (ISSUER, OTHER_ORCID)]


def test_hub_sign_in_uses_the_hub_issuer(db_engine, hub_tables, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from app.models.core_models import HubLoginTransaction
    from app.utils.hub_login import create_handoff_code

    monkeypatch.setenv("HUB_AUTH_ORCID_ISSUER", SANDBOX)
    now = datetime.now(timezone.utc)
    create_handoff_code(db_engine, HubLoginTransaction(state_hash="s", browser_secret_hash="b", client_id="crucible-hub", callback_uri="https://hub.example/cb", hub_state="h" * 16, code_challenge="c" * 43, nonce="n", created_at=now, expires_at=now + timedelta(minutes=10)), ORCID, None, None)
    assert identities(db_engine) == [(SANDBOX, ORCID)]


def test_apikey_page_writes_the_identity_row_with_the_older_flows_issuer(db_engine, monkeypatch):
    import time

    monkeypatch.setenv("ORCID_ISSUER", SANDBOX)
    browser = TestClient(app, base_url="https://testserver", follow_redirects=False)
    browser.cookies.set("crucible_user_token", encode_token({"sub": ORCID, "orcid": ORCID, "given_name": "Ada", "exp": int(time.time()) + 60}))
    assert browser.get("/auth/apikey").status_code == 200
    assert browser.get("/auth/apikey").status_code == 200
    assert identities(db_engine) == [(SANDBOX, ORCID)]


def test_post_users_with_orcid_writes_the_identity_row(client, db_engine, auth_as, monkeypatch):
    monkeypatch.setattr(users_routes, "find_or_add_api_access", lambda *args, **kwargs: "test-key")
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    response = client.post("/users", json={"user_info": {"first_name": "Ada", "last_name": "Lovelace", "username": "ada", "unique_id": ORCID}, "project_ids": []})
    assert response.status_code == 200, response.text
    assert identities(db_engine) == [(ISSUER, ORCID)]


def test_post_users_without_orcid_writes_no_identity_row(client, db_engine, auth_as, monkeypatch):
    monkeypatch.setattr(users_routes, "find_or_add_api_access", lambda *args, **kwargs: "test-key")
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    response = client.post("/users", json={"user_info": {"first_name": "Ada", "last_name": "Lovelace", "username": "ada"}, "project_ids": []})
    assert response.status_code == 200, response.text
    assert identities(db_engine) == []


def test_provisioning_uses_the_older_flows_issuer_when_the_hub_is_off(db_engine, monkeypatch):
    from app.utils.user_utils import provision_user

    monkeypatch.setenv("ORCID_ISSUER", SANDBOX)
    with Session(db_engine) as session, session.begin():
        provision_user(session, unique_id=ORCID, first_name="Ada", last_name="Lovelace", email=None, username=None)
    assert identities(db_engine) == [(SANDBOX, ORCID)]


def test_provisioning_uses_the_hub_issuer_when_the_hub_is_on(db_engine, monkeypatch):
    from app.utils.user_utils import provision_user

    monkeypatch.setenv("HUB_AUTH_ENABLED", "true")
    monkeypatch.setenv("HUB_AUTH_ORCID_ISSUER", SANDBOX)
    with Session(db_engine) as session, session.begin():
        provision_user(session, unique_id=ORCID, first_name="Ada", last_name="Lovelace", email=None, username=None)
    assert identities(db_engine) == [(SANDBOX, ORCID)]


def test_service_account_gets_no_identity_row(client, db_engine, auth_as):
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    response = client.post("/service_accounts", json={"username": "scope-one", "first_name": "Scope", "last_name": "One"})
    assert response.status_code in (200, 201), response.text
    assert identities(db_engine) == []


def seed_for_backfill(engine):
    with Session(engine) as session, session.begin():
        linked = add_user(session, unique_id=OTHER_ORCID)
        session.add(UserIdentity(user_id=linked.id, issuer=ISSUER, subject=OTHER_ORCID))
        add_user(session)
        add_user(session, unique_id=MFID)
        add_user(session, unique_id="1111-2222-3333-4444", is_service_account=True)


def load_backfill():
    path = Path(__file__).resolve().parents[1] / "migrations/data/20261103_backfill_user_identity.py"
    spec = importlib.util.spec_from_file_location("backfill_user_identity", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_backfill(*args):
    return load_backfill().main(list(args))


def test_backfill_requires_the_issuer():
    with pytest.raises(SystemExit):
        run_backfill()


def test_backfill_refuses_an_unknown_issuer(db_engine):
    assert run_backfill("--issuer", "https://example.org") == 1


def test_backfill_dry_run_writes_nothing(db_engine, capsys):
    seed_for_backfill(db_engine)
    assert run_backfill("--issuer", ISSUER) == 0
    assert identities(db_engine) == [(ISSUER, OTHER_ORCID)]
    out = capsys.readouterr().out
    assert "dry run" in out and "would add: 1" in out


def test_backfill_inserts_only_orcid_humans(db_engine, capsys):
    seed_for_backfill(db_engine)
    assert run_backfill("--issuer", ISSUER, "--commit") == 0
    assert sorted(identities(db_engine)) == sorted([(ISSUER, OTHER_ORCID), (ISSUER, ORCID)])
    out = capsys.readouterr().out
    assert "added: 1" in out and "already linked: 1" in out and "not an ORCID: 1" in out and "service accounts: 1" in out


def test_backfill_can_run_twice(db_engine, capsys):
    seed_for_backfill(db_engine)
    run_backfill("--issuer", ISSUER, "--commit")
    capsys.readouterr()
    assert run_backfill("--issuer", ISSUER, "--commit") == 0
    assert "added: 0" in capsys.readouterr().out
    assert len(identities(db_engine)) == 2


def test_backfill_reports_an_orcid_linked_to_someone_else_and_leaves_it(db_engine, capsys):
    with Session(db_engine) as session, session.begin():
        holder = add_user(session, unique_id=MFID)
        session.add(UserIdentity(user_id=holder.id, issuer=ISSUER, subject=ORCID))
        add_user(session)
    assert run_backfill("--issuer", ISSUER, "--commit") == 1
    assert "conflicts: 1" in capsys.readouterr().out
    assert identities(db_engine) == [(ISSUER, ORCID)]


def test_backfill_never_changes_a_user(db_engine):
    seed_for_backfill(db_engine)
    with Session(db_engine) as session:
        before = [(u.unique_id, u.first_name, u.email, u.username) for u in session.exec(select(User).order_by(User.id)).all()]
    run_backfill("--issuer", ISSUER, "--commit")
    with Session(db_engine) as session:
        assert before == [(u.unique_id, u.first_name, u.email, u.username) for u in session.exec(select(User).order_by(User.id)).all()]


@pytest.fixture
def pg_engine():
    value = os.getenv("HUB_AUTH_TEST_DATABASE_URL")
    if not value:
        pytest.skip("HUB_AUTH_TEST_DATABASE_URL requires an isolated local PostgreSQL database")
    url = make_url(value)
    if url.host not in {"127.0.0.1", "localhost"} or url.database != "crucible_hub_auth_test":
        pytest.fail("Use a local database named crucible_hub_auth_test")
    schema = "identity_test_" + uuid.uuid4().hex
    admin = create_engine(value)
    with admin.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(value, connect_args={"options": f"-csearch_path={schema}"})
    try:
        SQLModel.metadata.create_all(engine, tables=[User.__table__, UserIdentity.__table__])
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_constraints_hold_on_postgres(pg_engine):
    with Session(pg_engine) as session:
        user = add_user(session, unique_id=MFID)
        session.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=ORCID))
        session.flush()
        session.add(UserIdentity(user_id=user.id, issuer=SANDBOX, subject=OTHER_ORCID))
        with pytest.raises(IntegrityError):
            session.flush()


def test_concurrent_first_write_keeps_one_row_and_both_callers_succeed(pg_engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    with Session(pg_engine) as session, session.begin():
        user_id = add_user(session).id
    barrier = Barrier(4)

    def write(_):
        with Session(pg_engine) as session, session.begin():
            user = session.get(User, user_id)
            barrier.wait()
            return ensure_identity(session, user, ISSUER, ORCID).user_id

    with ThreadPoolExecutor(4) as pool:
        assert list(pool.map(write, range(4))) == [user_id] * 4
    assert identities(pg_engine) == [(ISSUER, ORCID)]


def test_deleting_a_user_removes_their_identities_on_postgres(pg_engine):
    with Session(pg_engine) as session, session.begin():
        user = add_user(session)
        session.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=ORCID))
    with pg_engine.begin() as connection:
        connection.execute(text('DELETE FROM "user"'))
    assert identities(pg_engine) == []
