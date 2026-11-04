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


@pytest.mark.parametrize("path", ["/auth/apikey", "/user_apikey"])
@pytest.mark.parametrize("email", [None, "ada@example.org"], ids=["html-page", "json"])
def test_apikey_page_is_never_stored(db_engine, path, email):
    with Session(db_engine) as session, session.begin():
        add_user(session).email = email
    response = signed_in_browser().get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html" if email is None else "application/json")
    assert response.headers["cache-control"] == "no-store"


def test_regenerated_apikey_is_never_stored(db_engine):
    with Session(db_engine) as session, session.begin():
        add_user(session)
    response = signed_in_browser().post("/auth/apikey/regenerate")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def signed_in_browser(orcid=ORCID):
    import time

    browser = TestClient(app, base_url="https://testserver", follow_redirects=False)
    browser.cookies.set("crucible_user_token", encode_token({"sub": orcid, "orcid": orcid, "given_name": "Ada", "exp": int(time.time()) + 60}))
    return browser


def link(engine, unique_id, subject, issuer=ISSUER):
    with Session(engine) as session, session.begin():
        ensure_identity(session, add_user(session, unique_id=unique_id), issuer, subject)


def users(engine):
    with Session(engine) as session:
        return sorted(session.exec(select(User.unique_id)).all())


def test_apikey_page_refuses_an_orcid_linked_to_another_user(db_engine):
    link(db_engine, MFID, ORCID)
    response = signed_in_browser().get("/auth/apikey")
    assert response.status_code == 409
    assert response.json()["detail"] == "This ORCID can't be used with this account; contact an administrator."
    assert users(db_engine) == [MFID]


def test_apikey_page_refuses_a_second_orcid_issuer_for_the_same_person(db_engine, monkeypatch):
    link(db_engine, ORCID, ORCID, issuer=SANDBOX)
    assert signed_in_browser().get("/auth/apikey").status_code == 409
    assert identities(db_engine) == [(SANDBOX, ORCID)]


def test_regenerate_refuses_an_orcid_linked_to_another_user(db_engine):
    link(db_engine, MFID, ORCID)
    assert signed_in_browser().post("/auth/apikey/regenerate").status_code == 409
    assert users(db_engine) == [MFID]


def test_regenerate_refuses_a_second_orcid_issuer_for_the_same_person(db_engine):
    link(db_engine, ORCID, ORCID, issuer=SANDBOX)
    assert signed_in_browser().post("/auth/apikey/regenerate").status_code == 409


def test_apikey_page_retries_when_a_simultaneous_sign_in_got_there_first(db_engine, monkeypatch):
    from app.api.routes import auth as auth_routes

    calls = []
    real = auth_routes.ensure_identity

    def lose_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return real(*args, **kwargs)

    monkeypatch.setattr(auth_routes, "ensure_identity", lose_once)
    assert signed_in_browser().get("/auth/apikey").status_code == 200
    assert len(calls) == 2
    assert identities(db_engine) == [(ISSUER, ORCID)]


def test_a_retry_that_fails_again_is_not_hidden(db_engine):
    from app.auth.identity import retry_after_concurrent_first_write

    calls = []

    def always_fails():
        calls.append(1)
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    with pytest.raises(IntegrityError):
        retry_after_concurrent_first_write(always_fails)
    assert len(calls) == 2


def new_user_body(**fields):
    return {"user_info": {"first_name": "Ada", "last_name": "Lovelace", "username": "ada", "unique_id": ORCID, **fields}, "project_ids": []}


def test_post_users_refuses_an_orcid_linked_to_another_user(client, db_engine, auth_as, monkeypatch):
    monkeypatch.setattr(users_routes, "find_mf_user_proposals", lambda orcid: pytest.fail("looked up proposals for a request that is refused"))
    link(db_engine, MFID, ORCID)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    response = client.post("/users", json=new_user_body())
    assert response.status_code == 409
    assert response.json()["detail"] == "This ORCID is already linked to another user"
    assert ORCID not in users(db_engine)


def test_post_users_refuses_an_identity_linked_between_the_check_and_the_write(client, db_engine, auth_as, monkeypatch):
    monkeypatch.setattr(users_routes, "find_mf_user_proposals", lambda orcid: link(db_engine, MFID, ORCID) or [])
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    assert client.post("/users", json=new_user_body()).status_code == 409
    assert ORCID not in users(db_engine)


@pytest.mark.parametrize("field", ["unique_id", "username"])
def test_post_users_does_not_look_up_proposals_for_a_duplicate(client, db_engine, auth_as, monkeypatch, field):
    monkeypatch.setattr(users_routes, "find_mf_user_proposals", lambda orcid: pytest.fail("looked up proposals for a duplicate"))
    with Session(db_engine) as session, session.begin():
        add_user(session, unique_id=ORCID if field == "unique_id" else OTHER_ORCID, username="ada")
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    assert client.post("/users", json=new_user_body()).status_code == 409


@pytest.mark.parametrize("constraint, expected", [
    ('duplicate key value violates unique constraint "ix_user_username"', {"code": "already_in_use", "fields": ["username"], "message": "Username already taken"}),
    ('duplicate key value violates unique constraint "ux_user_email_lower"', {"code": "already_in_use", "fields": ["email"], "message": "Email is already in use"}),
    ('duplicate key value violates unique constraint "ix_user_orcid"', "Another request created this user at the same time; nothing created"),
], ids=["username", "email", "identifier"])
def test_post_users_reports_a_simultaneous_create_as_a_conflict(client, db_engine, auth_as, monkeypatch, constraint, expected):
    def collides(*args, **kwargs):
        raise IntegrityError("insert", {}, Exception(constraint))

    monkeypatch.setattr(users_routes, "find_mf_user_proposals", lambda orcid: [])
    monkeypatch.setattr(users_routes, "provision_user", collides)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    response = client.post("/users", json=new_user_body())
    assert response.status_code == 409
    assert response.json()["detail"] == expected


def test_post_users_rechecks_the_username_after_the_proposal_lookup(client, db_engine, auth_as, monkeypatch):
    def taken_during_lookup(orcid):
        with Session(db_engine) as session, session.begin():
            add_user(session, unique_id=OTHER_ORCID, username="a_da")
        return []

    monkeypatch.setattr(users_routes, "find_mf_user_proposals", taken_during_lookup)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    response = client.post("/users", json=new_user_body())
    assert response.status_code == 409
    assert response.json()["detail"]["fields"] == ["username"]
    assert ORCID not in users(db_engine)


def test_post_users_does_not_hide_a_failure_that_is_not_a_conflict(client, db_engine, auth_as, monkeypatch):
    def fails(*args, **kwargs):
        raise IntegrityError("insert", {}, Exception('insert violates foreign key constraint "fk_group"'))

    monkeypatch.setattr(users_routes, "find_mf_user_proposals", lambda orcid: [])
    monkeypatch.setattr(users_routes, "provision_user", fails)
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)
    with pytest.raises(IntegrityError):
        client.post("/users", json=new_user_body())


def test_the_older_email_constraint_is_reported_as_an_email_conflict():
    from app.utils.user_utils import profile_conflict

    conflict = profile_conflict(IntegrityError("update", {}, Exception('duplicate key value violates unique constraint "uq_user_email"')))
    assert conflict.status_code == 409
    assert conflict.detail["fields"] == ["email"]


def test_admin_create_refuses_an_orcid_linked_to_another_user(db_engine):
    from app.cli.admin import Refused, _create

    link(db_engine, MFID, ORCID)
    with pytest.raises(Refused, match="already linked"):
        _create({"orcid": ORCID, "first_name": "Ada", "last_name": "Lovelace", "email": None, "username": "ada"}, allow_additional=True)
    assert users(db_engine) == [MFID]


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


@pytest.fixture
def pg_sign_in(pg_engine, monkeypatch):
    from app.models.core_models import AccessGroup, ApiToken, HubLoginCode, UserAccessGroupLink

    SQLModel.metadata.create_all(pg_engine, tables=[AccessGroup.__table__, UserAccessGroupLink.__table__, ApiToken.__table__, HubLoginCode.__table__])
    return pg_engine


def race(count, sign_in):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(count)

    def run(_):
        barrier.wait()
        return sign_in()

    with ThreadPoolExecutor(count) as pool:
        return list(pool.map(run, range(count)))


def assert_one_person(engine):
    from app.models.core_models import AccessGroup, UserAccessGroupLink

    with Session(engine) as session:
        assert session.exec(select(User.unique_id)).all() == [ORCID]
        assert session.exec(select(AccessGroup.group_name)).all() == [ORCID]
        assert len(session.exec(select(UserAccessGroupLink)).all()) == 1
    assert identities(engine) == [(ISSUER, ORCID)]


def test_simultaneous_first_apikey_sign_ins_create_one_person_on_postgres(pg_sign_in, monkeypatch):
    from threading import Barrier, BrokenBarrierError

    from app.api.routes import auth as auth_routes
    from app.auth.identity import retry_after_concurrent_first_write

    real_add_or_update = auth_routes.add_or_update
    all_found_no_user = Barrier(8)

    def add_after_every_thread_has_looked(*args, **kwargs):
        try:
            all_found_no_user.wait(timeout=2)
        except BrokenBarrierError:
            pass
        return real_add_or_update(*args, **kwargs)

    monkeypatch.setattr(auth_routes, "add_or_update", add_after_every_thread_has_looked)
    monkeypatch.setattr(auth_routes, "get_engine", lambda: pg_sign_in)
    claims = {"orcid": ORCID, "given_name": "Ada"}
    race(8, lambda: retry_after_concurrent_first_write(lambda: auth_routes._record_signed_in_user(ORCID, claims)))
    assert_one_person(pg_sign_in)


def test_simultaneous_first_hub_sign_ins_create_one_person_on_postgres(pg_sign_in):
    from types import SimpleNamespace

    from app.utils.hub_login import create_handoff_code

    transaction = SimpleNamespace(client_id="crucible-hub", callback_uri="https://hub.example/callback", code_challenge="c", hub_state="s")
    results = race(8, lambda: create_handoff_code(pg_sign_in, transaction, ORCID, "Ada", "Lovelace"))
    assert len({code for code, _ in results}) == 8
    assert_one_person(pg_sign_in)


def tokens(engine):
    from app.models.core_models import ApiToken

    with Session(engine) as session:
        return session.exec(select(ApiToken.token)).all()


@pytest.mark.parametrize("regenerate", [False, True], ids=["first-key", "regenerate"])
def test_simultaneous_key_requests_leave_one_key_on_postgres(pg_sign_in, monkeypatch, regenerate):
    from threading import Barrier, BrokenBarrierError

    from app.utils import auth_utils
    from app.utils.auth_utils import find_or_add_api_access

    with Session(pg_sign_in) as session, session.begin():
        add_user(session)
    if regenerate:
        find_or_add_api_access(ORCID, pg_sign_in)
    real_generate = auth_utils.generate_apikey
    all_found_no_key = Barrier(8)

    def generate_after_every_thread_has_looked(orcid, session):
        try:
            all_found_no_key.wait(timeout=1)
        except BrokenBarrierError:
            pass
        return real_generate(orcid, session)

    monkeypatch.setattr(auth_utils, "generate_apikey", generate_after_every_thread_has_looked)
    race(8, lambda: find_or_add_api_access(ORCID, pg_sign_in, regenerate=regenerate))
    assert len(tokens(pg_sign_in)) == 1
    assert find_or_add_api_access(ORCID, pg_sign_in) == tokens(pg_sign_in)[0]


def test_deleting_a_user_removes_their_identities_on_postgres(pg_engine):
    with Session(pg_engine) as session, session.begin():
        user = add_user(session)
        session.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=ORCID))
    with pg_engine.begin() as connection:
        connection.execute(text('DELETE FROM "user"'))
    assert identities(pg_engine) == []
