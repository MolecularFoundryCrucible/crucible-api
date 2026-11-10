"""resolve_user_by_identifier dispatches a single string to the right
resolve_user() lookup by format — the whole point of collapsing
TransferOwnershipRequest's three optional fields into one.
"""
import pytest
from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.tables import User
from app.utils.user_utils import resolve_user_by_identifier
from app.tables import UserIdentity

SA_MFID = "0tkn2knjast3h0008nyq9zps2c"  # real SA unique_id shape, see test_resource_access_routes.py


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[User.__table__, UserIdentity.__table__])
    with Session(engine) as session:
        session.add(User(first_name="A", last_name="B", email="alice@x.com",
                         unique_id="0000-0001-2345-6789", username="alice_b"))
        session.add(User(first_name="SA", last_name="Instrument", email=None,
                         unique_id=SA_MFID, is_service_account=True))
        session.commit()
        yield session


@pytest.fixture
def session():
    yield from _engine()


def test_resolves_by_orcid(session):
    user = resolve_user_by_identifier(session, "0000-0001-2345-6789")
    assert user.username == "alice_b"


def test_resolves_by_email(session):
    user = resolve_user_by_identifier(session, "alice@x.com")
    assert user.unique_id == "0000-0001-2345-6789"


def test_resolves_by_username(session):
    user = resolve_user_by_identifier(session, "alice_b")
    assert user.unique_id == "0000-0001-2345-6789"


def test_resolves_service_account_by_mfid(session):
    user = resolve_user_by_identifier(session, SA_MFID)
    assert user.is_service_account is True


def test_unknown_identifier_404s(session):
    with pytest.raises(HTTPException) as exc:
        resolve_user_by_identifier(session, "nobody_like_this")
    assert exc.value.status_code == 404
