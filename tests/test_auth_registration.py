import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest
from sqlmodel import Session, SQLModel, select

import tests.test_auth_providers as provider_tests
from app.api.main import app
from app.auth.registration import expire_pending_users
from app.auth.sign_in import NOT_LINKED, SignInRefused, resolve_sign_in
from app.tables import (
    DeviceAuthorization,
    LoginThrottle,
    AccessGroup, AuthEvent, Dataset, AccessToken, LoginCode, LoginTransaction, RefreshToken, TokenFamily, User, UserAccessGroupLink, UserIdentity,
)
from app.utils.auth_utils import authenticate_user
from app.auth.tokens import InvalidGrant, issue_tokens
from app.utils.permissions import PlatformRole
from tests.test_auth_providers import LAB_ENTRY, ORCID_ENTRY, set_providers

remote = provider_tests.remote
LAB_ISSUER = LAB_ENTRY["issuer"]
ADMIN = "0tmnyhfztdwc1000w2kspzsnh9"
SIGN_IN_CALLBACK = "https://crucible.lbl.gov/hub/auth/callback"


def sign_in(engine, issuer, subject, registration, **options):
    with Session(engine) as session, session.begin():
        user = resolve_sign_in(session, issuer, subject, "Ada", "Lovelace", registration=registration, **options)
        return user.unique_id, user.pending_at is not None


def people(engine):
    with Session(engine) as session:
        return {user.unique_id: user for user in session.exec(select(User)).all()}


def events(engine):
    with Session(engine) as session:
        return session.exec(select(AuthEvent.event)).all()


def test_closed_refuses_someone_new_and_creates_nothing(db_engine):
    for issuer, subject in ((LAB_ISSUER, "stranger"), ("https://orcid.org", "0000-0002-1825-0097")):
        with pytest.raises(SignInRefused) as caught:
            sign_in(db_engine, issuer, subject, "closed")
        assert caught.value.reason == NOT_LINKED
    assert people(db_engine) == {}


def test_approval_creates_a_waiting_account_with_its_login(db_engine):
    unique_id, pending = sign_in(db_engine, LAB_ISSUER, "newcomer", "approval")
    assert pending and len(unique_id) == 26
    orcid_id, orcid_pending = sign_in(db_engine, "https://orcid.org", "0000-0002-1825-0097", "approval")
    assert (orcid_id, orcid_pending) == ("0000-0002-1825-0097", True)
    with Session(db_engine) as session:
        assert {(row.issuer, row.subject) for row in session.exec(select(UserIdentity)).all()} == {(LAB_ISSUER, "newcomer"), ("https://orcid.org", "0000-0002-1825-0097")}
    assert events(db_engine) == ["registered", "registered"]
    assert sign_in(db_engine, LAB_ISSUER, "newcomer", "approval") == (unique_id, True)
    assert events(db_engine) == ["registered", "registered"]


def test_open_creates_a_working_account(db_engine):
    unique_id, pending = sign_in(db_engine, LAB_ISSUER, "newcomer", "open")
    assert not pending and len(unique_id) == 26
    assert events(db_engine) == ["sign_in"]


def test_waiting_accounts_get_no_hub_tokens_or_api_key(db_engine, use_engine):
    from fastapi import HTTPException
    from fastapi.security import HTTPAuthorizationCredentials

    from app.auth.api_keys import store_key
    from app.utils import auth_utils

    use_engine(db_engine)
    SQLModel.metadata.create_all(db_engine, tables=[LoginThrottle.__table__, DeviceAuthorization.__table__, TokenFamily.__table__, RefreshToken.__table__, AccessToken.__table__])
    unique_id, _ = sign_in(db_engine, LAB_ISSUER, "newcomer", "approval")
    user = people(db_engine)[unique_id]
    with pytest.raises(InvalidGrant):
        issue_tokens(db_engine, user.id, "crucible-hub")
    with Session(db_engine) as session:
        store_key(session, user.id, "planted")
        session.commit()
    with pytest.raises(HTTPException) as caught:
        auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials="planted"))
    assert caught.value.status_code == 401


@pytest.fixture
def hub(remote, client, db_engine, monkeypatch):
    import app.api.routes.browser_sign_in as hub_routes

    for name, value in {"HUB_AUTH_CALLBACK_URI": SIGN_IN_CALLBACK, "HUB_AUTH_PUBLIC_API_URL": "http://localhost:7777", "HUB_AUTH_ENABLED": "true",
                        "HUB_AUTH_CLIENT_SECRET": "test-only-secret-padded-to-32-characters", "CRUCIBLE_AUTH_IDENTITY_LOOKUP": "true"}.items():
        monkeypatch.setenv(name, value)
    set_providers(monkeypatch, ORCID_ENTRY, {**LAB_ENTRY, "registration": "approval"})
    monkeypatch.setattr(hub_routes, "get_api_root_path", lambda: "")
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (LoginTransaction, LoginCode, TokenFamily, RefreshToken, AccessToken)])
    return remote, client


def hub_sign_in(hub, subject, *, valid=True):
    fakes, client = hub
    started = client.get("/auth/hub/login", params={"client_id": "crucible-hub", "redirect_uri": SIGN_IN_CALLBACK, "state": "hub-state-0123456789", "code_challenge": "a" * 43,
                                                    "code_challenge_method": "S256", "provider": "lab"}, follow_redirects=False)
    query = parse_qs(urlparse(started.headers["location"]).query)
    fakes["login.example.org"].claims = {"nonce": query["nonce"][0] if valid else "wrong", "sub": subject, "auth_time": int(time.time())}
    return client.get("/oauth/callback/lab", params={"code": "c", "state": query["state"][0]}, follow_redirects=False)


def test_hub_sign_in_shows_the_waiting_page_and_issues_no_code(hub, db_engine):
    response = hub_sign_in(hub, "newcomer")
    assert response.status_code == 403
    assert "waiting for approval" in response.text
    assert response.headers["content-security-policy"].startswith("default-src 'none'")
    with Session(db_engine) as session:
        assert session.exec(select(LoginCode)).all() == []
    assert [user.pending_at is not None for user in people(db_engine).values()] == [True]


def test_waiting_is_shown_only_after_a_valid_sign_in(hub, db_engine):
    hub_sign_in(hub, "newcomer")
    refused = hub_sign_in(hub, "newcomer", valid=False)
    assert refused.status_code == 302 and "waiting" not in refused.text
    assert parse_qs(urlparse(refused.headers["location"]).query)["error"] == ["server_error"]


def test_approved_account_signs_in_through_the_hub(hub, db_engine, auth_as):
    hub_sign_in(hub, "newcomer")
    _, client = hub
    admin(db_engine, auth_as, client)
    unique_id = next(iter(u for u in people(db_engine) if u != ADMIN))
    assert client.post(f"/users/{unique_id}/approve").status_code == 200
    app.dependency_overrides.pop(authenticate_user, None)
    finished = hub_sign_in(hub, "newcomer")
    assert finished.status_code == 302 and "code" in parse_qs(urlparse(finished.headers["location"]).query)
    assert events(db_engine) == ["registered", "approved", "sign_in"]


def admin(engine, auth_as, client):
    with Session(engine) as session:
        if session.exec(select(User).where(User.unique_id == ADMIN)).first() is None:
            session.add(User(unique_id=ADMIN, username="admin", first_name="A", last_name="D", email=None, platform_role="admin"))
            session.commit()
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)


def waiting(engine, subject="newcomer"):
    unique_id, _ = sign_in(engine, LAB_ISSUER, subject, "approval")
    return unique_id


def test_waiting_accounts_are_hidden_from_everyone_but_administrators(client, db_engine, auth_as):
    unique_id = waiting(db_engine)
    username = people(db_engine)[unique_id].username
    with Session(db_engine) as session:
        session.add(User(unique_id="0tmnyhfztdwc1000w2kspzsnh5", username="member", first_name="M", last_name="E", email=None))
        session.commit()
    auth_as("0tmnyhfztdwc1000w2kspzsnh5")
    assert client.get(f"/users/{unique_id}").status_code == 404
    assert client.get(f"/users/{username}").status_code == 404
    assert unique_id not in [item["unique_id"] for item in client.get("/users").json()["items"]]
    assert client.get("/users", params={"pending": "true"}).status_code == 403
    assert client.get("/datasets", params={"owner_id": unique_id}).status_code == 200
    admin(db_engine, auth_as, client)
    assert unique_id not in [item["unique_id"] for item in client.get("/users").json()["items"]]
    pending = client.get("/users", params={"pending": "true"}).json()["items"]
    assert [(item["unique_id"], item["pending_at"] is not None) for item in pending] == [(unique_id, True)]


def test_approve_and_reject_only_waiting_accounts(client, db_engine, auth_as):
    first, second = waiting(db_engine, "first"), waiting(db_engine, "second")
    admin(db_engine, auth_as, client)
    approved = client.post(f"/users/{first}/approve")
    assert (approved.status_code, approved.json()["pending_at"]) == (200, None)
    for route in ("approve", "reject"):
        response = client.post(f"/users/{first}/{route}")
        assert (response.status_code, response.json()["detail"]["code"]) == (409, "not_pending")
    assert client.post(f"/users/{second}/reject").status_code == 204
    assert set(people(db_engine)) == {first, ADMIN}
    with Session(db_engine) as session:
        assert {row.subject for row in session.exec(select(UserIdentity)).all()} == {"first"}
        assert session.exec(select(AccessGroup).where(AccessGroup.group_name == second)).first() is None
        assert session.exec(select(UserAccessGroupLink).where(UserAccessGroupLink.accessgroup_id == None)).all() == []  # noqa: E711
    assert events(db_engine) == ["registered", "registered", "approved", "rejected"]
    again, still_waiting = sign_in(db_engine, LAB_ISSUER, "second", "approval")
    assert again != second and still_waiting


def test_reject_with_unexpected_references_changes_nothing(client, db_engine, auth_as):
    unique_id = waiting(db_engine)
    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == unique_id)).one()
        session.add(Dataset(unique_id="ds-pending", dataset_name="d", owner_orcid=unique_id, owner_user_id=user.id))
        session.commit()
    admin(db_engine, auth_as, client)
    response = client.post(f"/users/{unique_id}/reject")
    assert (response.status_code, response.json()["detail"]["code"]) == (409, "still_referenced")
    assert "datasets" in response.json()["detail"]["message"]
    assert unique_id in people(db_engine)


def test_switching_to_closed_keeps_waiting_accounts_reviewable(client, db_engine, auth_as, monkeypatch):
    unique_id = waiting(db_engine)
    set_providers(monkeypatch, ORCID_ENTRY, {**LAB_ENTRY, "registration": "closed"})
    admin(db_engine, auth_as, client)
    assert [item["unique_id"] for item in client.get("/users", params={"pending": "true"}).json()["items"]] == [unique_id]
    assert client.post(f"/users/{unique_id}/approve").status_code == 200


def test_stale_waiting_accounts_expire_through_the_same_function(db_engine):
    stale, fresh = waiting(db_engine, "stale"), waiting(db_engine, "fresh")
    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == stale)).one()
        user.pending_at = datetime.now(timezone.utc) - timedelta(days=31)
        session.add(user)
        session.commit()
    assert expire_pending_users(db_engine) == 1
    assert set(people(db_engine)) == {stale, fresh}
    assert expire_pending_users(db_engine, commit=True) == 1
    assert set(people(db_engine)) == {fresh}
    assert events(db_engine)[-1] == "rejected"


def test_cleanup_job_reports_and_removes_stale_waiting_accounts(db_engine):
    from app.auth.cleanup import cleanup_sign_in_records

    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (LoginTransaction, LoginCode, TokenFamily, RefreshToken, AccessToken, LoginThrottle, DeviceAuthorization)])
    waiting(db_engine)
    later = datetime.now(timezone.utc) + timedelta(days=31)
    assert cleanup_sign_in_records(db_engine, now=later)["pending_users"] == 1
    assert cleanup_sign_in_records(db_engine, commit=True, now=later)["pending_users"] == 1
    assert people(db_engine) == {}


def test_approving_or_rejecting_from_a_session_needs_a_recent_sign_in(client, db_engine, auth_as):
    from datetime import datetime, timedelta, timezone

    waiting_one = waiting(db_engine, "late")
    admin(db_engine, auth_as, client)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN, credential="session", authenticated_at=datetime.now(timezone.utc) - timedelta(hours=1))
    for route in ("approve", "reject"):
        refused = client.post(f"/users/{waiting_one}/{route}")
        assert (refused.status_code, refused.json()["detail"]["code"]) == (401, "insufficient_user_authentication")
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN, credential="session", authenticated_at=datetime.now(timezone.utc))
    assert client.post(f"/users/{waiting_one}/approve").status_code == 200
