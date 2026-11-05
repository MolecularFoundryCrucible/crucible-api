from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlmodel import Session, SQLModel, select

from app.auth.sign_in import DISABLED, SignInRefused, resolve_sign_in
from app.models.core_models import (
    AuthEvent, HubAccessToken, HubLoginCode, HubLoginTransaction, HubRefreshToken, HubTokenFamily, User, UserIdentity,
)
from app.utils import auth_utils
from app.utils.auth_utils import add_apikey
from app.utils.hub_login import HubLoginRefused, create_handoff_code, create_login, consume_login, exchange_handoff_code, HubLoginConfig
from app.utils.hub_tokens import InvalidHubGrant, hub_access_family, issue_hub_tokens, refresh_hub_tokens
from app.utils.permissions import PlatformRole
from tests.test_hub_tokens import ORCID_PROVIDER
from tests.people import internal_id, person_row

ORCID = "0000-0002-1825-0097"
ADMIN = "0tmnyhfztdwc1000w2kspzsnh0"
OTHER_ADMIN = "0tmnyhfztdwc1000w2kspzsnh1"
ISSUER = "https://orcid.org"
NOW = datetime.now(timezone.utc)


@pytest.fixture(autouse=True)
def hub_tables(db_engine):
    SQLModel.metadata.create_all(db_engine, tables=[model.__table__ for model in (HubLoginCode, HubLoginTransaction, HubTokenFamily, HubRefreshToken, HubAccessToken)])
    with auth_utils._auth_cache_lock:
        auth_utils._auth_cache.clear()


def person(db_engine, unique_id=ORCID, disabled=False, **fields):
    with Session(db_engine) as session, session.begin():
        user = User(**{"unique_id": unique_id, "first_name": "Ada", "last_name": "Lovelace", "email": None, **fields})
        user.disabled_at = NOW if disabled else None
        session.add(user)
        session.flush()
        if unique_id == ORCID:
            session.add(UserIdentity(user_id=user.id, issuer=ISSUER, subject=ORCID))
    return unique_id


def set_disabled(db_engine, unique_id=ORCID, value=NOW):
    with Session(db_engine) as session, session.begin():
        session.exec(select(User).where(User.unique_id == unique_id)).one().disabled_at = value


def api_key_for(db_engine, unique_id=ORCID):
    with Session(db_engine) as session, session.begin():
        return add_apikey(person_row(session, unique_id), session)


def authorize(token):
    return auth_utils._authorize_user(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))


def login_record():
    return HubLoginTransaction(state_hash="s", browser_secret_hash="b", client_id="crucible-hub", callback_uri="https://hub.example/cb", hub_state="h" * 16, code_challenge="c" * 43, nonce="n", created_at=NOW, expires_at=NOW + timedelta(minutes=10))


def test_disabled_refused_at_the_resolver_and_never_continues(db_engine):
    person(db_engine, disabled=True)
    with Session(db_engine) as session, session.begin():
        with pytest.raises(SignInRefused) as caught:
            resolve_sign_in(session, ISSUER, ORCID, "Ada", "L")
    assert caught.value.reason == DISABLED
    with Session(db_engine) as session:
        assert session.exec(select(UserIdentity)).one().verified_at is None


def test_disabled_refused_at_the_resolver_with_the_lookup_on(db_engine, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("CRUCIBLE_AUTH_IDENTITY_LOOKUP", "true")
    get_settings.cache_clear()
    test_disabled_refused_at_the_resolver_and_never_continues(db_engine)


def test_disabled_refused_at_the_hub_callback(db_engine):
    person(db_engine, disabled=True)
    with pytest.raises(HubLoginRefused) as caught:
        create_handoff_code(db_engine, login_record(), ORCID_PROVIDER, ORCID, None, None)
    assert caught.value.error == "access_denied"
    with Session(db_engine) as session:
        assert session.exec(select(HubLoginCode)).all() == []


def test_disabled_refused_on_handoff_redemption(db_engine):
    import base64
    import hashlib

    person(db_engine)
    config = HubLoginConfig("crucible-hub", "https://hub.example/cb", "https://api.example")
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state, secret, _ = create_login(db_engine, config, "crucible-hub", "https://hub.example/cb", "hub-state-123456789", challenge)
    code, _ = create_handoff_code(db_engine, consume_login(db_engine, state, secret), ORCID_PROVIDER, ORCID, None, None)
    set_disabled(db_engine)
    with pytest.raises(InvalidHubGrant):
        exchange_handoff_code(db_engine, code, verifier, "crucible-hub", "https://hub.example/cb")


def test_disabled_refused_on_hub_refresh_and_the_family_is_revoked(db_engine):
    person(db_engine)
    pair = issue_hub_tokens(db_engine, internal_id(db_engine, ORCID), "crucible-hub")
    set_disabled(db_engine)
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(db_engine, pair.refresh_token, "crucible-hub")
    with Session(db_engine) as session:
        assert session.exec(select(HubTokenFamily)).one().revoked_at is not None


def test_disabled_refused_on_hub_access_token(db_engine):
    person(db_engine)
    pair = issue_hub_tokens(db_engine, internal_id(db_engine, ORCID), "crucible-hub")
    set_disabled(db_engine)
    with Session(db_engine) as session:
        with pytest.raises(InvalidHubGrant):
            hub_access_family(session, pair.access_token)
    with pytest.raises(HTTPException) as caught:
        authorize(pair.access_token)
    assert caught.value.status_code == 401


def test_disabled_refused_on_api_key_after_cache_clear(db_engine):
    person(db_engine)
    key = api_key_for(db_engine)
    assert authorize(key).user_id == ORCID
    set_disabled(db_engine)
    with auth_utils._auth_cache_lock:
        auth_utils._auth_cache.clear()
    with pytest.raises(HTTPException) as caught:
        authorize(key)
    assert caught.value.status_code == 401
    assert caught.value.detail == "Invalid or expired API key."


def test_load_principal_still_works_for_a_disabled_target(db_engine):
    person(db_engine, disabled=True)
    with Session(db_engine) as session:
        assert auth_utils.load_principal(session, internal_id(session, ORCID)).user_id == ORCID


def as_admin(auth_as, client, user_id=ADMIN, age=timedelta(seconds=5)):
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    return auth_as(user_id, platform_role=PlatformRole.ADMIN, credential="hub", authenticated_at=datetime.now(timezone.utc) - age)


def human_admins(db_engine, *ids):
    for unique_id in ids:
        person(db_engine, unique_id, platform_role="admin")


def test_admin_disables_and_everything_stops_at_once(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN)
    person(db_engine)
    key = api_key_for(db_engine)
    assert authorize(key).user_id == ORCID
    pair = issue_hub_tokens(db_engine, internal_id(db_engine, ORCID), "crucible-hub")
    as_admin(auth_as, client)
    response = client.post(f"/users/{ORCID}/disable")
    assert response.status_code == 200, response.text
    assert response.json()["disabled_at"] is not None
    with pytest.raises(HTTPException):
        authorize(key)
    with Session(db_engine) as session:
        assert session.exec(select(HubTokenFamily)).one().revoked_at is not None
        event = session.exec(select(AuthEvent)).one()
        admin_id = session.exec(select(User.id).where(User.unique_id == ADMIN)).one()
        assert (event.event, event.user_unique_id, event.actor_user_id) == ("disabled", ORCID, admin_id)
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(db_engine, pair.refresh_token, "crucible-hub")


def test_enable_restores_sign_in_but_not_old_sessions(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN)
    person(db_engine)
    key = api_key_for(db_engine)
    pair = issue_hub_tokens(db_engine, internal_id(db_engine, ORCID), "crucible-hub")
    as_admin(auth_as, client)
    assert client.post(f"/users/{ORCID}/disable").status_code == 200
    response = client.post(f"/users/{ORCID}/enable")
    assert response.status_code == 200, response.text
    assert response.json()["disabled_at"] is None
    assert authorize(key).user_id == ORCID
    with pytest.raises(InvalidHubGrant):
        refresh_hub_tokens(db_engine, pair.refresh_token, "crucible-hub")
    with Session(db_engine) as session:
        assert [row for row in session.exec(select(AuthEvent.event).order_by(AuthEvent.id))] == ["disabled", "enabled"]


def test_disable_twice_keeps_the_first_time_and_one_event(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN)
    person(db_engine)
    as_admin(auth_as, client)
    first = client.post(f"/users/{ORCID}/disable").json()["disabled_at"]
    assert client.post(f"/users/{ORCID}/disable").json()["disabled_at"] == first
    with Session(db_engine) as session:
        assert len(session.exec(select(AuthEvent)).all()) == 1


def test_enable_when_active_writes_no_event(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN)
    person(db_engine)
    as_admin(auth_as, client)
    assert client.post(f"/users/{ORCID}/enable").status_code == 200
    with Session(db_engine) as session:
        assert session.exec(select(AuthEvent)).all() == []


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_non_admin_is_refused(client, auth_as, db_engine, action):
    person(db_engine)
    auth_as("0000-0000-0000-0002")
    assert client.post(f"/users/{ORCID}/{action}").status_code == 403


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_service_account_admin_is_refused(client, auth_as, db_engine, action):
    person(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN, is_service_account=True)
    assert client.post(f"/users/{ORCID}/{action}").status_code == 403


def test_hub_administrator_must_ask_for_elevated_mode(client, auth_as, db_engine):
    person(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN, credential="hub", authenticated_at=datetime.now(timezone.utc))
    assert client.post(f"/users/{ORCID}/disable").status_code == 403


def test_admin_cannot_disable_themselves(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN)
    as_admin(auth_as, client)
    assert client.post(f"/users/{ADMIN}/disable").status_code == 403


def test_api_key_admin_can_disable_an_ordinary_person(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN)
    person(db_engine)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert client.post(f"/users/{ORCID}/disable").status_code == 200


@pytest.mark.parametrize("action", ["disable", "enable"])
def test_api_key_cannot_change_a_human_administrator(client, auth_as, db_engine, action):
    human_admins(db_engine, ADMIN, OTHER_ADMIN)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert client.post(f"/users/{OTHER_ADMIN}/{action}").status_code == 403


def test_stale_hub_session_gets_a_step_up_for_a_human_administrator(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN, OTHER_ADMIN)
    as_admin(auth_as, client, age=timedelta(hours=1))
    response = client.post(f"/users/{OTHER_ADMIN}/disable")
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "insufficient_user_authentication"


def test_recent_hub_session_can_disable_another_administrator(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN, OTHER_ADMIN)
    as_admin(auth_as, client)
    assert client.post(f"/users/{OTHER_ADMIN}/disable").status_code == 200


def test_cannot_disable_the_last_active_human_administrator(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN, OTHER_ADMIN)
    set_disabled(db_engine, ADMIN)
    as_admin(auth_as, client)
    response = client.post(f"/users/{OTHER_ADMIN}/disable")
    assert response.status_code == 409


def test_service_account_can_be_disabled(client, auth_as, db_engine):
    human_admins(db_engine, ADMIN)
    person(db_engine, OTHER_ADMIN, is_service_account=True)
    key = api_key_for(db_engine, OTHER_ADMIN)
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert client.post(f"/users/{OTHER_ADMIN}/disable").status_code == 200
    with pytest.raises(HTTPException):
        authorize(key)


def test_unknown_person_is_404(client, auth_as, db_engine):
    as_admin(auth_as, client)
    assert client.post("/users/0000-0000-0000-0009/disable").status_code == 404
