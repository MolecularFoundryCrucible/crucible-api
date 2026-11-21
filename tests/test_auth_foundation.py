import inspect
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from app.auth.events import AuthEventType, record_event
from app.auth.identity import orcid_of
from app.tables import AccessGroup, AuthEvent, LoginTransaction, User, UserIdentity
from app.tables import LoginCode
from app.utils.user_utils import generate_unique_username, provision_user
from tests.test_hub_tokens import ORCID_PROVIDER
from sqlmodel import SQLModel

ORCID = "0000-0002-1825-0097"
MFID = "0tmnyhfztdwc1000w2kspzsnh0"


def person(session, unique_id=MFID, username="ada"):
    user = User(unique_id=unique_id, first_name="Ada", last_name="Lovelace", username=username)
    session.add(user)
    session.flush()
    return user


def test_record_event_writes_a_row_in_the_callers_transaction(db_engine):
    with Session(db_engine) as session:
        user = person(session)
        admin = person(session, "0tmnyhfztdwc1000w2kspzsnh1", "admin")
        record_event(session, AuthEventType.DISABLED, user=user, actor=admin, issuer="https://orcid.org", client_address="192.0.2.1")
        session.rollback()
    with Session(db_engine) as session:
        assert session.exec(select(AuthEvent)).all() == []
        user = person(session)
        admin = person(session, "0tmnyhfztdwc1000w2kspzsnh1", "admin")
        record_event(session, AuthEventType.DISABLED, user=user, actor=admin, issuer="https://orcid.org", client_address="192.0.2.1")
        session.commit()
        row = session.exec(select(AuthEvent)).one()
        assert (row.event, row.user_id, row.user_unique_id, row.actor_user_id, row.issuer, row.client_address) == ("disabled", user.id, MFID, admin.id, "https://orcid.org", "192.0.2.1")
        assert row.at is not None


def test_record_event_without_a_user_is_allowed(db_engine):
    with Session(db_engine) as session:
        record_event(session, AuthEventType.SIGN_IN_FAILED)
        session.commit()
        row = session.exec(select(AuthEvent)).one()
        assert row.user_id is None and row.user_unique_id is None


def test_auth_event_never_stores_secrets_by_signature():
    assert set(inspect.signature(record_event).parameters) == {"session", "event", "user", "actor", "issuer", "client_address", "client_id"}
    assert set(AuthEvent.model_fields) == {"id", "at", "event", "user_id", "user_unique_id", "actor_user_id", "issuer", "client_address", "client_id"}


def test_every_event_name_fits_the_column():
    assert AuthEventType.DISABLED.value == "disabled"
    assert all(len(event.value) <= 32 for event in AuthEventType)


def test_orcid_of_reads_the_linked_orcid(db_engine):
    with Session(db_engine) as session:
        user = person(session)
        session.add(UserIdentity(user_id=user.id, issuer="https://orcid.org", subject=ORCID))
        session.commit()
        assert orcid_of(session, user) == ORCID


def test_orcid_of_ignores_orcid_shaped_unique_id(db_engine):
    with Session(db_engine) as session:
        assert orcid_of(session, person(session, unique_id=ORCID)) is None


def test_orcid_of_ignores_other_issuers(db_engine):
    with Session(db_engine) as session:
        user = person(session)
        session.add(UserIdentity(user_id=user.id, issuer="https://login.example.org", subject=ORCID))
        session.commit()
        assert orcid_of(session, user) is None


@pytest.mark.parametrize("first_name,last_name,given", [("Ada", "Lovelace", None), ("Ada", "Lovelace", ""), ("", "", None)])
def test_provision_generates_a_username_when_none_is_given(db_engine, first_name, last_name, given):
    with Session(db_engine) as session:
        user = provision_user(session, unique_id=MFID, first_name=first_name, last_name=last_name, email=None, username=given)
        assert user.username


def test_provision_keeps_a_given_username(db_engine):
    with Session(db_engine) as session:
        assert provision_user(session, unique_id=MFID, first_name="Ada", last_name="Lovelace", email=None, username="countess").username == "countess"


def test_provision_avoids_a_taken_username(db_engine):
    with Session(db_engine) as session:
        first = provision_user(session, unique_id=MFID, first_name="Ada", last_name="Lovelace", email=None, username=None)
        second = provision_user(session, unique_id="0tmnyhfztdwc1000w2kspzsnh1", first_name="Ada", last_name="Lovelace", email=None, username=None)
        assert first.username != second.username


def test_provision_uses_the_given_orcid_issuer(db_engine):
    with Session(db_engine) as session:
        provision_user(session, unique_id=ORCID, first_name="Ada", last_name="L", email=None, username=None, orcid_issuer="https://sandbox.orcid.org")
        session.commit()
        assert session.exec(select(UserIdentity.issuer)).all() == ["https://sandbox.orcid.org"]


def test_provision_reuses_an_existing_identity_group(db_engine):
    with Session(db_engine) as session:
        session.add(AccessGroup(group_name=ORCID, group_type="identity"))
        session.commit()
        provision_user(session, unique_id=ORCID, first_name="Ada", last_name="L", email=None, username=None)
        session.commit()
        assert len(session.exec(select(AccessGroup).where(AccessGroup.group_name == ORCID)).all()) == 1


@pytest.fixture
def hub_tables(db_engine):
    SQLModel.metadata.create_all(db_engine, tables=[LoginCode.__table__, LoginTransaction.__table__])


def login_record(state="s"):
    now = datetime.now(timezone.utc)
    return LoginTransaction(state_hash=state, browser_secret_hash="b", client_id="crucible-hub", callback_uri="https://hub.example/cb", hub_state="h" * 16, code_challenge="c" * 43, nonce="n", created_at=now, expires_at=now + timedelta(minutes=10))


def test_new_person_from_hub_sign_in_gets_a_username_and_identity_group(db_engine, hub_tables):
    from app.auth.login_transactions import create_handoff_code

    create_handoff_code(db_engine, login_record(), ORCID_PROVIDER, ORCID, "Ada", "Lovelace")
    with Session(db_engine) as session:
        user = session.exec(select(User)).one()
        assert user.username == generate_unique_username(session, "Ada", "Lovelace").removesuffix("_2")
        assert session.exec(select(AccessGroup.group_type).where(AccessGroup.group_name == ORCID)).one() == "identity"


def test_existing_person_from_hub_sign_in_keeps_their_username(db_engine, hub_tables):
    from app.auth.login_transactions import create_handoff_code

    with Session(db_engine) as session:
        session.add(User(unique_id=ORCID, first_name="Ada", last_name="Lovelace", username="countess"))
        session.commit()
    create_handoff_code(db_engine, login_record(), ORCID_PROVIDER, ORCID, "Ada", "Lovelace")
    with Session(db_engine) as session:
        assert session.exec(select(User)).one().username == "countess"


def test_existing_person_from_hub_sign_in_only_fills_empty_names(db_engine, hub_tables):
    from app.auth.login_transactions import create_handoff_code

    with Session(db_engine) as session:
        session.add(User(unique_id=ORCID, first_name="Kept", last_name=""))
        session.commit()
    create_handoff_code(db_engine, login_record(), ORCID_PROVIDER, ORCID, "Ada", "Lovelace")
    with Session(db_engine) as session:
        user = session.exec(select(User)).one()
        assert (user.first_name, user.last_name) == ("Kept", "Lovelace")
