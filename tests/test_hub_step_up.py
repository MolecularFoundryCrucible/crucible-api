from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app.api.main import app
from app.models.core_models import ApiToken, User
from app.utils.auth_utils import STEP_UP_MAX_AGE, authenticate_user
from app.utils.permissions import PlatformRole, Principal
from app.auth.api_keys import store_key
from tests.people import internal_id

ORCID = "0000-0000-0000-0001"
OTHER = "0000-0000-0000-0002"
FRESH = "fresh"
STALE = "stale"


@pytest.fixture
def act_as(db_engine):
    with Session(db_engine) as session:
        session.add_all([User(unique_id=ORCID, first_name="A", last_name="B"), User(unique_id=OTHER, first_name="C", last_name="D")])
        session.commit()

    def _set(credential="api_key", signed_in=FRESH, admin=False):
        authenticated_at = None
        if credential == "session" and signed_in is not None:
            age = timedelta(seconds=30) if signed_in == FRESH else timedelta(seconds=STEP_UP_MAX_AGE + 1)
            authenticated_at = datetime.now(timezone.utc) - age
        principal = Principal(user_id=ORCID, internal_user_id=internal_id(db_engine, ORCID), platform_role=PlatformRole.ADMIN if admin else PlatformRole.NONE, credential=credential, authenticated_at=authenticated_at)
        app.dependency_overrides[authenticate_user] = lambda: principal

    yield _set
    app.dependency_overrides.pop(authenticate_user, None)


def add_key(engine, orcid, token):
    with Session(engine) as session:
        session.add(ApiToken(user_id=internal_id(session, orcid), user_unique_id=orcid, access_group_id=orcid, token=token, token_created=datetime.now(timezone.utc).date().isoformat()))
        store_key(session, internal_id(session, orcid), token)
        session.commit()


def keys(engine, orcid):
    with Session(engine) as session:
        return [row.token for row in session.exec(select(ApiToken).where(ApiToken.user_unique_id == orcid)).all()]


def assert_step_up(response):
    assert response.status_code == 401
    assert response.json()["detail"] == {"code": "insufficient_user_authentication", "max_age": STEP_UP_MAX_AGE}
    challenge = response.headers["www-authenticate"]
    assert 'error="insufficient_user_authentication"' in challenge
    assert f'max_age="{STEP_UP_MAX_AGE}"' in challenge


def test_hub_sessions_default_to_normal_privilege_mode(client, act_as):
    act_as("session", admin=True)
    assert client.get("/account/profile").json()["authorization"]["effective_privilege_mode"] == "normal"
    elevated = client.get("/account/profile", headers={"Crucible-Privilege-Mode": "elevated"})
    assert elevated.json()["authorization"]["effective_privilege_mode"] == "elevated"
    act_as("api_key", admin=True)
    assert client.get("/account/profile").json()["authorization"]["effective_privilege_mode"] == "elevated"


def test_service_account_keys_require_a_recent_sign_in_for_hub_sessions(client, act_as):
    act_as("session", signed_in=STALE, admin=True)
    headers = {"Crucible-Privilege-Mode": "elevated"}
    assert_step_up(client.post("/service_accounts", json={"username": "robot"}, headers=headers))
    assert_step_up(client.post("/service_accounts/robot/rotate_key", headers=headers))
    act_as("session", admin=True)
    assert client.post("/service_accounts", json={"username": "robot"}, headers=headers).status_code == 200
