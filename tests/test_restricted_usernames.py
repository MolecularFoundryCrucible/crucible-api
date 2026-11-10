import pytest
from sqlmodel import Session, select

from app.tables import User
from app.utils.permissions import PlatformRole
from app.utils.user_utils import RESTRICTED_USERNAMES, generate_unique_username, is_restricted_username

ME = "0tmnyhfztdwc1000w2kspzsnh0"
ADMIN = "0tmnyhfztdwc1000w2kspzsnh9"


@pytest.fixture
def people(db_engine):
    with Session(db_engine) as session:
        session.add(User(unique_id=ME, username="ada", first_name="Ada", last_name="L", email=None))
        session.add(User(unique_id=ADMIN, username="boss", first_name="B", last_name="O", email=None, platform_role="admin"))
        session.add(User(unique_id="0tmnyhfztdwc1000w2kspzsnh1", username="support", first_name="S", last_name="U", email=None))
        session.commit()
    return db_engine


def username_of(engine, unique_id):
    with Session(engine) as session:
        return session.exec(select(User.username).where(User.unique_id == unique_id)).one()


@pytest.mark.parametrize("name", ["admin", "Admin", "ad_min", "ad-min", "by_username", "root"])
def test_restricted_check_catches_lookalikes(name):
    assert is_restricted_username(name)


def test_ordinary_names_are_not_restricted():
    assert not any(is_restricted_username(name) for name in ("ada", "adminton", "rooted", "hubert"))


def test_choosing_your_own_restricted_username_is_refused(client, auth_as, people):
    auth_as(ME)
    for route in ("/account/profile", f"/users/{ME}"):
        response = client.patch(route, json={"username": "ad_min"})
        assert (response.status_code, response.json()["detail"]["code"]) == (409, "username_restricted"), route
    assert username_of(people, ME) == "ada"
    assert client.patch("/account/profile", json={"username": "ada_lovelace"}).status_code == 200


def test_admin_can_set_a_restricted_username(client, auth_as, people):
    client.headers["Crucible-Privilege-Mode"] = "elevated"
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)
    assert client.patch(f"/users/{ME}", json={"username": "security"}).status_code == 200
    assert client.patch("/account/profile", json={"username": "admin"}).status_code == 200
    assert (username_of(people, ME), username_of(people, ADMIN)) == ("security", "admin")


def test_existing_holder_can_save_their_profile(client, auth_as, people):
    auth_as("0tmnyhfztdwc1000w2kspzsnh1")
    assert client.patch("/account/profile", json={"username": "support", "first_name": "Sam"}).status_code == 200


def test_generated_username_avoids_the_list(db_engine):
    with Session(db_engine) as session:
        assert generate_unique_username(session, "", "") == "user"
        assert not is_restricted_username(generate_unique_username(session, "Ad", "Min"))
        assert generate_unique_username(session, "Root", "") != "root"
    assert "admin" in RESTRICTED_USERNAMES
