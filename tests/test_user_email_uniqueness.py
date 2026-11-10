import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.routes import account as account_routes
from app.tables import User
from app.utils.permissions import PlatformRole

ME = "0000-0000-0000-0001"
OTHER = "0000-0000-0000-0002"


@pytest.fixture
def users(db_engine):
    with Session(db_engine) as session:
        session.add_all([User(unique_id=ME, first_name="A", last_name="B", email="me@lbl.gov"), User(unique_id=OTHER, first_name="C", last_name="D", email="taken@lbl.gov")])
        session.commit()
    return db_engine


def email_of(engine, orcid):
    with Session(engine) as session:
        return session.exec(select(User.email).where(User.unique_id == orcid)).one()


@pytest.mark.parametrize("claimed", ["taken@lbl.gov", "  Taken@LBL.gov "])
def test_profile_cannot_claim_another_users_email(client, auth_as, users, claimed):
    auth_as(ME)
    response = client.patch("/account/profile", json={"email": claimed})
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "already_in_use", "fields": ["email"], "message": "Email is already in use"}
    assert OTHER not in response.text
    assert email_of(users, ME) == "me@lbl.gov"


def test_profile_email_is_normalized_and_self_updates_are_allowed(client, auth_as, users):
    auth_as(ME)
    assert client.patch("/account/profile", json={"email": " ME@lbl.gov "}).status_code == 200
    assert email_of(users, ME) == "me@lbl.gov"
    assert client.patch("/account/profile", json={"email": "   "}).status_code == 200
    assert email_of(users, ME) is None
    assert client.patch("/account/profile", json={"email": "new@lbl.gov"}).status_code == 200
    assert email_of(users, ME) == "new@lbl.gov"


def test_admin_update_and_creation_reject_taken_emails(client, auth_as, users):
    auth_as(ME, platform_role=PlatformRole.ADMIN)
    assert client.patch(f"/users/{ME}", json={"email": "TAKEN@lbl.gov"}).status_code == 409
    created = client.post("/users", json={"user_info": {"first_name": "E", "last_name": "F", "username": "newperson", "email": "taken@lbl.gov"}, "project_ids": []})
    assert created.status_code == 409, created.text
    assert created.json()["detail"]["fields"] == ["email"]


def test_concurrent_claim_is_rejected_by_the_database(client, auth_as, users, monkeypatch):
    monkeypatch.setattr(account_routes, "assert_profile_fields_available", lambda *args, **kwargs: None)
    auth_as(ME)
    response = client.patch("/account/profile", json={"email": "taken@lbl.gov"})
    assert response.status_code == 409
    assert response.json()["detail"]["fields"] == ["email"]
    assert email_of(users, ME) == "me@lbl.gov"


def test_index_allows_many_null_emails_but_no_case_duplicates(db_engine):
    with Session(db_engine) as session:
        session.add_all([User(unique_id=f"0000-0000-0000-000{n}", first_name="", last_name="") for n in range(3)])
        session.commit()
        session.add_all([User(unique_id="0000-0000-0000-0010", first_name="", last_name="", email="x@lbl.gov"), User(unique_id="0000-0000-0000-0011", first_name="", last_name="", email="X@lbl.gov")])
        with pytest.raises(IntegrityError):
            session.commit()


def test_username_and_email_conflicts_are_reported_together(client, auth_as, users):
    with Session(users) as session:
        other = session.exec(select(User).where(User.unique_id == OTHER)).one()
        other.username = "taken"
        session.add(other)
        session.commit()
    auth_as(ME)
    response = client.patch("/account/profile", json={"username": "taken", "email": "taken@lbl.gov"})
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "already_in_use", "fields": ["username", "email"], "message": "Username and email are already in use"}
    response = client.patch("/account/profile", json={"username": "taken"})
    assert response.json()["detail"] == {"code": "already_in_use", "fields": ["username"], "message": "Username already taken"}
