from datetime import date, timedelta

import pytest
from sqlmodel import Session

from app.models.core_models import ApiToken, User
from app.utils.auth_utils import authenticate_user
from app.utils.permissions import Principal

ME = "0000-0008-0000-0001"
OLD_KEY, NEW_KEY = "o" * 64, "n" * 64


@pytest.fixture
def account(client, db_engine, auth_as):
    with Session(db_engine) as session:
        session.add(User(first_name="Key", last_name="Holder", email=None, unique_id=ME, username="keyholder"))
        session.commit()
    return client


def add_key(db_engine, token, created):
    with Session(db_engine) as session:
        session.add(ApiToken(token=token, user_unique_id=ME, access_group_id=ME, token_created=created.isoformat()))
        session.commit()


def as_caller(credential):
    from app.api.main import app
    app.dependency_overrides[authenticate_user] = lambda: Principal(user_id=ME, credential=credential)


def verify(client, token="chba_session"):
    return client.get("/account/verify", headers={"Authorization": f"Bearer {token}"})


def test_api_key_callers_get_the_key_they_presented(account, db_engine):
    add_key(db_engine, OLD_KEY, date.today() - timedelta(days=100))
    add_key(db_engine, NEW_KEY, date.today() - timedelta(days=10))
    as_caller("api_key")
    body = verify(account, OLD_KEY).json()
    assert body["valid"] is True
    assert body["created_at"] == (date.today() - timedelta(days=100)).isoformat()
    assert body["expires_at"] == (date.today() + timedelta(days=265)).isoformat()


def test_hub_callers_get_the_newest_key(account, db_engine):
    add_key(db_engine, OLD_KEY, date.today() - timedelta(days=100))
    add_key(db_engine, NEW_KEY, date.today() - timedelta(days=10))
    as_caller("hub")
    body = verify(account).json()
    assert (body["valid"], body["created_at"]) == (True, (date.today() - timedelta(days=10)).isoformat())


def test_hub_callers_see_an_expired_key_as_invalid(account, db_engine):
    add_key(db_engine, OLD_KEY, date.today() - timedelta(days=400))
    as_caller("hub")
    body = verify(account).json()
    assert body["valid"] is False
    assert body["expires_at"] == (date.today() - timedelta(days=35)).isoformat()


def test_a_key_expires_on_its_first_anniversary(account, db_engine):
    add_key(db_engine, OLD_KEY, date.today() - timedelta(days=365))
    as_caller("hub")
    assert verify(account).json()["valid"] is False


def test_hub_callers_without_a_key_get_404(account):
    as_caller("hub")
    assert verify(account).status_code == 404
