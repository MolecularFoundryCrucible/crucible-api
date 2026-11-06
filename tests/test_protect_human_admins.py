from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app.cli import admin as admin_cli
from app.models.core_models import AuthEvent, User
from app.utils import auth_utils
from app.utils.permissions import PlatformRole

ADMIN = "0tmnyhfztdwc1000w2kspzsnh0"
OTHER_ADMIN = "0tmnyhfztdwc1000w2kspzsnh1"
PERSON = "0tmnyhfztdwc1000w2kspzsnh2"
ROBOT = "0tmnyhfztdwc1000w2kspzsnh3"


@pytest.fixture
def people(db_engine):
    with Session(db_engine) as session:
        for unique_id, username, role, service in (
            (ADMIN, "admin", "admin", False), (OTHER_ADMIN, "other", "admin", False),
            (PERSON, "person", "none", False), (ROBOT, "robot", "contributor", True),
        ):
            session.add(User(unique_id=unique_id, username=username, first_name="A", last_name="B", email=f"{username}@example.com",
                             platform_role=role, is_service_account=service))
        session.commit()
    return db_engine


def as_key(auth_as, client):
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)


def as_hub(auth_as, client, age=timedelta(seconds=5)):
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN, credential="hub", authenticated_at=datetime.now(timezone.utc) - age)


def role(client, ref, value):
    return client.patch(f"/users/{ref}/platform_role", json={"platform_role": value})


def refused_for_a_key(response):
    return response.status_code == 403 and response.json()["detail"]["code"] == "hub_session_required"


def asks_to_sign_in_again(response):
    return response.status_code == 401 and response.json()["detail"]["code"] == "insufficient_user_authentication"


def test_api_key_cannot_change_a_humans_admin_status(client, auth_as, people):
    as_key(auth_as, client)
    assert refused_for_a_key(role(client, OTHER_ADMIN, "none"))
    assert refused_for_a_key(role(client, PERSON, "admin"))
    assert role(client, PERSON, "contributor").status_code == 200


def test_api_key_cannot_read_or_regenerate_an_admins_key_even_its_own(client, auth_as, people):
    as_key(auth_as, client)
    for ref in (OTHER_ADMIN, ADMIN):
        assert refused_for_a_key(client.post(f"/users/{ref}/apikey"))
        assert refused_for_a_key(client.post(f"/users/{ref}/apikey/regenerate"))


def test_api_key_still_manages_ordinary_users_and_service_accounts(client, auth_as, people):
    as_key(auth_as, client)
    assert client.post(f"/users/{PERSON}/apikey/regenerate").status_code == 200
    assert client.delete(f"/users/{PERSON}/apikey").status_code == 204
    assert client.patch(f"/service_accounts/{ROBOT}", json={"platform_role": "admin"}).status_code == 200
    assert client.patch(f"/users/{PERSON}", json={"username": "renamed", "email": "renamed@example.com"}).status_code == 200
    assert client.patch("/account/profile", json={"username": "myself"}).status_code == 200


def test_recent_hub_session_can_do_all_of_these(client, auth_as, people):
    as_hub(auth_as, client)
    assert role(client, PERSON, "admin").status_code == 200
    assert role(client, PERSON, "none").status_code == 200
    assert client.post(f"/users/{OTHER_ADMIN}/apikey/regenerate").status_code == 200


def test_stale_hub_session_is_asked_to_sign_in_again(client, auth_as, people):
    as_hub(auth_as, client, age=timedelta(hours=1))
    assert asks_to_sign_in_again(role(client, OTHER_ADMIN, "none"))
    assert asks_to_sign_in_again(role(client, PERSON, "admin"))
    assert asks_to_sign_in_again(client.post(f"/users/{PERSON}/disable"))
    assert role(client, PERSON, "contributor").status_code == 200


def test_role_change_is_recorded_and_drops_cached_keys(client, auth_as, people, monkeypatch):
    forgotten = []
    monkeypatch.setattr("app.api.routes.users.forget_cached_keys", lambda session, user_id: forgotten.append(user_id))
    as_hub(auth_as, client)
    assert role(client, OTHER_ADMIN, "contributor").status_code == 200
    with Session(people) as session:
        demoted = session.exec(select(User).where(User.unique_id == OTHER_ADMIN)).one()
        actor = session.exec(select(User.id).where(User.unique_id == ADMIN)).one()
        event = session.exec(select(AuthEvent)).one()
    assert forgotten == [demoted.id]
    assert (event.event, event.user_id, event.actor_user_id) == ("role_changed", demoted.id, actor)


def test_demoted_admin_key_loses_admin_rights_at_once(people, use_engine):
    use_engine(people)
    with Session(people) as session:
        user = session.exec(select(User).where(User.unique_id == OTHER_ADMIN)).one()
        key = auth_utils.find_or_add_api_access(user.id, people)
    from fastapi.security import HTTPAuthorizationCredentials
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials=key)
    assert auth_utils._authorize_user(credentials).is_platform_admin
    assert admin_cli._run_set_role(type("Args", (), {"user": OTHER_ADMIN, "role": "none"})()) == 0
    assert not auth_utils._authorize_user(credentials).is_platform_admin


def test_cli_set_role_changes_roles_and_keeps_the_last_admin(people, use_engine, capsys):
    use_engine(people)
    run = lambda ref, value: admin_cli._run_set_role(type("Args", (), {"user": ref, "role": value})())  # noqa: E731
    assert run("person@example.com", "admin") == 0
    assert run("person", "none") == 0
    assert run(OTHER_ADMIN, "none") == 0
    assert run("admin", "none") == 1
    assert "last active human platform administrator" in capsys.readouterr().err
    assert run("robot", "admin") == 1
    assert run("nobody", "admin") == 1
    with Session(people) as session:
        roles = dict(session.exec(select(User.unique_id, User.platform_role)).all())
        events = session.exec(select(AuthEvent.event)).all()
    assert (roles[ADMIN], roles[OTHER_ADMIN], roles[PERSON], roles[ROBOT]) == ("admin", "none", "none", "contributor")
    assert events == ["role_changed"] * 3


def test_api_key_cannot_edit_or_revoke_the_keys_of_another_admin(client, auth_as, people):
    as_key(auth_as, client)
    assert refused_for_a_key(client.patch(f"/users/{OTHER_ADMIN}", json={"email": "takeover@example.com"}))
    assert refused_for_a_key(client.delete(f"/users/{OTHER_ADMIN}/apikey"))
    assert client.patch(f"/users/{PERSON}", json={"first_name": "Changed"}).status_code == 200
    assert client.delete(f"/users/{PERSON}/apikey").status_code == 204
    assert client.patch(f"/users/{ADMIN}", json={"email": "mine@example.com"}).status_code == 200
    with Session(people) as session:
        assert session.exec(select(User.email).where(User.unique_id == OTHER_ADMIN)).one() == "other@example.com"


def test_recent_hub_session_can_edit_another_admin(client, auth_as, people):
    as_hub(auth_as, client)
    assert client.patch(f"/users/{OTHER_ADMIN}", json={"first_name": "Renamed"}).status_code == 200
    assert client.delete(f"/users/{OTHER_ADMIN}/apikey").status_code == 204
    as_hub(auth_as, client, age=timedelta(hours=1))
    assert asks_to_sign_in_again(client.patch(f"/users/{OTHER_ADMIN}", json={"first_name": "Again"}))
