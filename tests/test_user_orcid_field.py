from datetime import datetime, timezone

import pytest
from sqlmodel import Session

from app.config import get_settings
from app.models.core_models import Dataset, Resource, User, UserIdentity
from app.utils.permissions import PlatformRole

ORCID = "0000-0002-1825-0097"
OTHER_ORCID = "0000-0001-5109-3700"
MFID = "0tmnyhfztdwc1000w2kspzsnh9"
LAB = "https://login.lab.example"


@pytest.fixture
def lookup(monkeypatch):
    def set_lookup(on: bool):
        monkeypatch.setenv("CRUCIBLE_AUTH_IDENTITY_LOOKUP", "true" if on else "false")
        get_settings.cache_clear()
    yield set_lookup
    get_settings.cache_clear()


def _person(db_engine, unique_id, username, *, orcid=None, verified=False, issuer="https://orcid.org", service=False):
    with Session(db_engine) as session:
        user = User(unique_id=unique_id, first_name="A", last_name="B", email=None, username=username, is_service_account=service)
        session.add(user)
        session.flush()
        if orcid is not None:
            session.add(UserIdentity(user_id=user.id, issuer=issuer, subject=orcid, verified_at=datetime.now(timezone.utc) if verified else None))
        session.commit()
        return user.id


def _profile(client, username):
    body = client.get(f"/users/{username}").json()
    return body["orcid"], body["orcid_verified"]


def test_linked_orcid_shows_with_its_verified_status(client, db_engine, auth_as, lookup):
    lookup(True)
    _person(db_engine, MFID, "ada", orcid=ORCID, verified=True)
    _person(db_engine, "0tmnyhfztdwc1000w2kspzsnh8", "bob", orcid=OTHER_ORCID)
    auth_as("viewer", platform_role=PlatformRole.NONE)
    assert _profile(client, "ada") == (ORCID, True)
    assert _profile(client, "bob") == (OTHER_ORCID, False)


def test_no_orcid_means_null_fields(client, db_engine, auth_as, lookup):
    lookup(True)
    _person(db_engine, MFID, "ada")
    _person(db_engine, "0tmnyhfztdwc1000w2kspzsnh8", "lab", orcid=ORCID, issuer=LAB)
    _person(db_engine, "0tmnyhfztdwc1000w2kspzsnh7", "robot", service=True)
    auth_as("viewer", platform_role=PlatformRole.NONE)
    for username in ("ada", "lab", "robot"):
        assert _profile(client, username) == (None, None), username


def test_orcid_shaped_unique_id_counts_only_until_the_lookup_setting_is_on(client, db_engine, auth_as, lookup):
    _person(db_engine, ORCID, "ada")
    auth_as("viewer", platform_role=PlatformRole.NONE)
    lookup(False)
    assert _profile(client, "ada") == (ORCID, False)
    lookup(True)
    assert _profile(client, "ada") == (None, None)


def test_owner_objects_carry_the_orcid(client, db_engine, auth_as, lookup):
    lookup(True)
    user_id = _person(db_engine, MFID, "ada", orcid=ORCID, verified=True)
    with Session(db_engine) as session:
        session.add_all([Dataset(unique_id="ds", dataset_name="d", owner_orcid=MFID, owner_user_id=user_id), Resource(mfid="ds", resource_type="dataset")])
        session.commit()
    auth_as("viewer", platform_role=PlatformRole.ADMIN)
    owner = client.get("/resources/ds").json()["owner"]
    assert (owner["unique_id"], owner["orcid"], owner["orcid_verified"]) == (MFID, ORCID, True)
