from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, SQLModel, select

from app.auth.sign_in import IDENTITY_TAKEN, NOT_LINKED, SERVICE_ACCOUNT, UNLINKED_ORCID, WRONG_USER, SignInRefused, resolve_sign_in
from app.config import get_settings
from app.tables import AccessGroup, AuthEvent, HubLoginCode, HubLoginTransaction, User, UserAccessGroupLink, UserIdentity
from app.utils.hub_login import HubLoginRefused, create_handoff_code
from tests.test_auth_providers import LAB_ENTRY
from tests.test_hub_tokens import ORCID_PROVIDER
from app.config import OidcProvider

ORCID_ISSUER = "https://orcid.org"
LAB_ISSUER = LAB_ENTRY["issuer"]
ORCID = "0000-0002-1825-0097"
MFID = "0tmnyhfztdwc1000w2kspzsnh0"
LAB = OidcProvider(**LAB_ENTRY)


@pytest.fixture
def lookup_on(monkeypatch):
    monkeypatch.setenv("CRUCIBLE_AUTH_IDENTITY_LOOKUP", "true")
    get_settings.cache_clear()


def person(db_engine, unique_id=MFID, issuer=None, subject=None, **fields):
    with Session(db_engine) as session, session.begin():
        user = User(**{"unique_id": unique_id, "first_name": "Ada", "last_name": "Lovelace", "email": None, **fields})
        session.add(user)
        session.flush()
        if issuer:
            session.add(UserIdentity(user_id=user.id, issuer=issuer, subject=subject))
    return unique_id


def registration_of(issuer):
    return LAB_ENTRY["registration"] if issuer == LAB_ISSUER else "open"


def sign_in(db_engine, issuer, subject, **options):
    with Session(db_engine) as session, session.begin():
        return resolve_sign_in(session, issuer, subject, "Ada", "Lovelace", **{"registration": registration_of(issuer), **options}).unique_id


def refused(db_engine, issuer, subject, **options):
    with pytest.raises(SignInRefused) as caught:
        sign_in(db_engine, issuer, subject, **options)
    return caught.value.reason


def people(db_engine):
    with Session(db_engine) as session:
        return sorted(session.exec(select(User.unique_id)).all())


def identity_of(db_engine, subject):
    with Session(db_engine) as session:
        return session.exec(select(UserIdentity).where(UserIdentity.subject == subject)).one()


def test_known_identity_signs_in(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user")
    assert sign_in(db_engine, LAB_ISSUER, "lab-user") == MFID


def test_linked_orcid_finds_a_person_whose_id_is_an_mfid(db_engine, lookup_on):
    person(db_engine, MFID, ORCID_ISSUER, ORCID)
    assert sign_in(db_engine, ORCID_ISSUER, ORCID) == MFID
    assert people(db_engine) == [MFID]


def test_foundry_user_signs_in_and_keeps_unique_id_after_backfill(db_engine, lookup_on):
    person(db_engine, ORCID, ORCID_ISSUER, ORCID)
    assert sign_in(db_engine, ORCID_ISSUER, ORCID) == ORCID


def test_unlinked_orcid_is_refused(db_engine, lookup_on):
    person(db_engine, ORCID)
    assert refused(db_engine, ORCID_ISSUER, ORCID) == UNLINKED_ORCID
    assert people(db_engine) == [ORCID]


def test_flag_off_keeps_looking_up_unique_id(db_engine):
    person(db_engine, ORCID)
    assert sign_in(db_engine, ORCID_ISSUER, ORCID) == ORCID
    assert identity_of(db_engine, ORCID).issuer == ORCID_ISSUER


def test_flag_off_refuses_an_orcid_linked_to_someone_else(db_engine):
    person(db_engine, MFID, ORCID_ISSUER, ORCID)
    assert refused(db_engine, ORCID_ISSUER, ORCID) == IDENTITY_TAKEN
    assert people(db_engine) == [MFID]


def test_flag_off_other_providers_still_use_the_identity_table(db_engine):
    person(db_engine, MFID, LAB_ISSUER, "lab-user")
    assert sign_in(db_engine, LAB_ISSUER, "lab-user") == MFID


@pytest.mark.parametrize("flag", [False, True])
def test_new_orcid_user_unique_id_is_the_orcid(db_engine, monkeypatch, flag):
    monkeypatch.setenv("CRUCIBLE_AUTH_IDENTITY_LOOKUP", str(flag).lower())
    get_settings.cache_clear()
    assert sign_in(db_engine, ORCID_ISSUER, ORCID) == ORCID
    with Session(db_engine) as session:
        user = session.exec(select(User)).one()
        assert user.username
        group = session.exec(select(AccessGroup).where(AccessGroup.group_name == ORCID)).one()
        assert session.get(UserAccessGroupLink, (user.id, group.id)) is not None


def test_stranger_at_another_provider_is_refused_and_creates_nothing(db_engine, lookup_on):
    assert refused(db_engine, LAB_ISSUER, "stranger") == NOT_LINKED
    assert people(db_engine) == []


def test_orcid_shaped_sub_from_other_provider_gets_no_orcid_treatment(db_engine, lookup_on):
    person(db_engine, ORCID)
    assert refused(db_engine, LAB_ISSUER, ORCID) == NOT_LINKED


def test_badly_shaped_subject_at_orcid_is_refused(db_engine, lookup_on):
    assert refused(db_engine, ORCID_ISSUER, "not-an-orcid") == NOT_LINKED
    assert people(db_engine) == []


def test_no_match_by_email_when_emails_equal(db_engine, lookup_on):
    person(db_engine, MFID, email="ada@example.org")
    assert refused(db_engine, LAB_ISSUER, "ada@example.org") == NOT_LINKED


def test_no_match_by_username(db_engine, lookup_on):
    person(db_engine, MFID, username="ada")
    assert refused(db_engine, LAB_ISSUER, "ada") == NOT_LINKED


def test_same_subject_at_another_issuer_stays_separate(db_engine, lookup_on):
    person(db_engine, MFID, "https://other.example", "shared")
    assert refused(db_engine, LAB_ISSUER, "shared") == NOT_LINKED


def test_service_account_is_refused(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user", is_service_account=True)
    assert refused(db_engine, LAB_ISSUER, "lab-user") == SERVICE_ACCOUNT


def test_required_user_match_signs_in(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user")
    assert sign_in(db_engine, LAB_ISSUER, "lab-user", required_user=MFID) == MFID


def test_required_user_mismatch_is_refused(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user")
    assert refused(db_engine, LAB_ISSUER, "lab-user", required_user="someone-else") == WRONG_USER


@pytest.mark.parametrize("flag", [False, True])
def test_required_user_never_creates_anyone(db_engine, monkeypatch, flag):
    monkeypatch.setenv("CRUCIBLE_AUTH_IDENTITY_LOOKUP", str(flag).lower())
    get_settings.cache_clear()
    assert refused(db_engine, ORCID_ISSUER, ORCID, required_user=MFID) == WRONG_USER
    assert people(db_engine) == []


def test_sign_in_sets_verified_at_and_last_used_at(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user")
    assert identity_of(db_engine, "lab-user").verified_at is None
    sign_in(db_engine, LAB_ISSUER, "lab-user")
    first = identity_of(db_engine, "lab-user")
    assert first.verified_at is not None and first.last_used_at is not None
    sign_in(db_engine, LAB_ISSUER, "lab-user")
    second = identity_of(db_engine, "lab-user")
    assert second.verified_at == first.verified_at
    assert second.last_used_at >= first.last_used_at


def test_refused_sign_in_changes_nothing(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user")
    refused(db_engine, LAB_ISSUER, "lab-user", required_user="someone-else")
    identity = identity_of(db_engine, "lab-user")
    assert identity.verified_at is None and identity.last_used_at is None


def test_existing_names_are_kept_and_empty_ones_filled(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user", first_name="Kept", last_name="")
    sign_in(db_engine, LAB_ISSUER, "lab-user")
    with Session(db_engine) as session:
        user = session.exec(select(User)).one()
        assert (user.first_name, user.last_name) == ("Kept", "Lovelace")


def test_sign_in_writes_one_event(db_engine, lookup_on):
    person(db_engine, MFID, LAB_ISSUER, "lab-user")
    sign_in(db_engine, LAB_ISSUER, "lab-user")
    with Session(db_engine) as session:
        event = session.exec(select(AuthEvent)).one()
        assert (event.event, event.user_unique_id, event.issuer) == ("sign_in", MFID, LAB_ISSUER)


def transaction(required_user=None):
    now = datetime.now(timezone.utc)
    return HubLoginTransaction(state_hash="s", browser_secret_hash="b", client_id="crucible-hub", callback_uri="https://hub.example/cb", hub_state="h" * 16, code_challenge="c" * 43, nonce="n", required_user=required_user, created_at=now, expires_at=now + timedelta(minutes=10))


@pytest.fixture
def hub_tables(db_engine):
    SQLModel.metadata.create_all(db_engine, tables=[HubLoginCode.__table__, HubLoginTransaction.__table__])


def test_hub_uses_the_resolver_for_an_mfid_person(db_engine, hub_tables, lookup_on):
    person(db_engine, MFID, ORCID_ISSUER, ORCID)
    create_handoff_code(db_engine, transaction(), ORCID_PROVIDER, ORCID, None, None)
    with Session(db_engine) as session:
        assert session.exec(select(HubLoginCode.user_unique_id)).one() == MFID


@pytest.mark.parametrize("required, error", [(None, "access_denied"), ("someone-else", "login_required")])
def test_hub_refusal_keeps_the_event_and_issues_no_code(db_engine, hub_tables, lookup_on, required, error):
    with pytest.raises(HubLoginRefused) as caught:
        create_handoff_code(db_engine, transaction(required), LAB, "stranger", None, None)
    assert caught.value.error == error
    with Session(db_engine) as session:
        assert session.exec(select(HubLoginCode)).all() == []
        event = session.exec(select(AuthEvent)).one()
        assert (event.event, event.user_id, event.issuer) == ("provider_refused", None, LAB_ISSUER)


def test_unlinked_orcid_at_the_hub_is_access_denied(db_engine, hub_tables, lookup_on):
    person(db_engine, ORCID)
    with pytest.raises(HubLoginRefused) as caught:
        create_handoff_code(db_engine, transaction(), ORCID_PROVIDER, ORCID, None, None)
    assert caught.value.error == "access_denied"

