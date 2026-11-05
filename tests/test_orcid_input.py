import pytest
from sqlmodel import Session

from app.models.core_models import Dataset, Resource, Sample, User, UserIdentity
from app.utils.permissions import PlatformRole
from app.utils.user_utils import canonical_person_id, resolve_user, resolve_user_by_identifier, user_by_id_or_orcid
from app.utils.user_utils import provision_user

ORCID = "0000-0002-1825-0097"
OTHER_ORCID = "0000-0001-5109-3700"
MFID = "0tmnyhfztdwc1000w2kspzsnh0"
ADMIN = "0000-0000-0000-0001"
ORCID_ISSUER = "https://orcid.org"


@pytest.fixture
def person(db_engine):
    with Session(db_engine) as session, session.begin():
        user = provision_user(session, unique_id=MFID, first_name="Ada", last_name="Lovelace", email="ada@example.org", username="ada")
        session.add(UserIdentity(user_id=user.id, issuer=ORCID_ISSUER, subject=ORCID))


@pytest.fixture
def admin(client, auth_as, db_engine):
    with Session(db_engine) as session, session.begin():
        provision_user(session, unique_id=ADMIN, first_name="A", last_name="B", email=None, username="admin")
    auth_as(ADMIN, platform_role=PlatformRole.ADMIN)


def test_linked_orcid_finds_the_person(db_engine, person):
    with Session(db_engine) as session:
        assert user_by_id_or_orcid(session, ORCID).unique_id == MFID
        assert user_by_id_or_orcid(session, MFID).unique_id == MFID
        assert resolve_user(session, orcid=ORCID).unique_id == MFID
        assert resolve_user_by_identifier(session, ORCID).unique_id == MFID


def test_orcid_as_unique_id_still_works_without_a_row(db_engine):
    with Session(db_engine) as session, session.begin():
        session.add(User(unique_id=ORCID, first_name="A", last_name="B", email=None))
    with Session(db_engine) as session:
        assert user_by_id_or_orcid(session, ORCID).unique_id == ORCID


def test_unknown_orcid_finds_nobody(db_engine, person):
    with Session(db_engine) as session:
        assert user_by_id_or_orcid(session, OTHER_ORCID) is None
        assert canonical_person_id(session, OTHER_ORCID) == OTHER_ORCID
        assert canonical_person_id(session, None) is None


def test_orcid_shaped_subject_from_another_provider_finds_nobody(db_engine):
    with Session(db_engine) as session, session.begin():
        user = User(unique_id=MFID, first_name="A", last_name="B", email=None)
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, issuer="https://login.example.org", subject=ORCID))
    with Session(db_engine) as session:
        assert user_by_id_or_orcid(session, ORCID) is None


def test_subject_that_is_not_orcid_shaped_is_never_looked_up(db_engine):
    with Session(db_engine) as session, session.begin():
        user = User(unique_id=MFID, first_name="A", last_name="B", email=None)
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, issuer=ORCID_ISSUER, subject="not-an-orcid"))
    with Session(db_engine) as session:
        assert user_by_id_or_orcid(session, "not-an-orcid") is None


def test_get_user_by_orcid(client, admin, person):
    response = client.get(f"/users/{ORCID}")
    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == MFID


def test_routes_taking_a_user_ref_accept_the_orcid(client, admin, person):
    assert client.get(f"/users/{ORCID}/access_groups").status_code == 200
    assert client.get(f"/users/{ORCID}/projects").status_code == 200
    assert client.patch(f"/users/{ORCID}", json={"first_name": "Augusta"}).json()["unique_id"] == MFID


def test_unknown_orcid_is_404(client, admin):
    assert client.get(f"/users/{OTHER_ORCID}").status_code == 404


def test_user_list_filter_accepts_the_orcid(client, admin, person):
    items = client.get("/users", params={"unique_id": ORCID}).json()["items"]
    assert [item["unique_id"] for item in items] == [MFID]
    assert [item["unique_id"] for item in client.get("/users", params={"unique_id": MFID}).json()["items"]] == [MFID]


def test_user_list_filter_ignores_another_providers_subject(client, admin, db_engine, person):
    with Session(db_engine) as session, session.begin():
        user = User(unique_id="0tmnyhfztdwc1000w2kspzsnh1", first_name="A", last_name="B", email=None)
        session.add(user)
        session.flush()
        session.add(UserIdentity(user_id=user.id, issuer="https://login.example.org", subject=OTHER_ORCID))
    assert client.get("/users", params={"unique_id": OTHER_ORCID}).json()["items"] == []


def test_batch_resolve_accepts_the_orcid(client, admin, person):
    response = client.post("/users/resolve", json={"orcids": [ORCID]})
    assert response.status_code == 200, response.text
    assert list(response.json()) == [MFID]


def test_creating_with_owner_as_the_orcid(client, admin, person):
    response = client.post("/datasets", json={"dataset_name": "x", "owner": ORCID})
    assert response.status_code in (200, 201), response.text
    assert response.json()["owner_orcid"] == MFID


def test_project_lead_as_the_orcid(client, admin, person):
    response = client.post("/projects", json={"project_id": "MF-77", "organization": "MF", "project_lead": ORCID})
    assert response.status_code in (200, 201), response.text
    assert response.json()["project_lead_orcid"] == MFID


def seed_records(db_engine):
    with Session(db_engine) as session, session.begin():
        session.add(Dataset(unique_id="0tdatasetmine00000000000a0", dataset_name="mine", owner_orcid=MFID))
        session.add(Dataset(unique_id="0tdatasetothr00000000000a0", dataset_name="other", owner_orcid="someone"))
        session.add(Sample(unique_id="0tsamplemine000000000000a0", sample_name="mine", owner_orcid=MFID))
        session.add(Sample(unique_id="0tsampleothr000000000000a0", sample_name="other", owner_orcid="someone"))
        for mfid, kind in (("0tdatasetmine00000000000a0", "dataset"), ("0tdatasetothr00000000000a0", "dataset"), ("0tsamplemine000000000000a0", "sample"), ("0tsampleothr000000000000a0", "sample")):
            session.add(Resource(mfid=mfid, resource_type=kind))


@pytest.mark.parametrize("path, param", [("/datasets", "owner_id"), ("/datasets", "owner_orcid"), ("/samples", "owner_id"), ("/samples", "owner_orcid")])
def test_owner_filter_accepts_the_orcid(client, admin, db_engine, person, path, param):
    seed_records(db_engine)
    by_orcid = client.get(path, params={param: ORCID})
    assert by_orcid.status_code == 200, by_orcid.text
    assert [item["owner_orcid"] for item in by_orcid.json()["items"]] == [MFID]
    assert [item["owner_orcid"] for item in client.get(path, params={param: MFID}).json()["items"]] == [MFID]


def test_owner_filter_with_an_unknown_value_matches_nothing(client, admin, db_engine, person):
    seed_records(db_engine)
    assert client.get("/datasets", params={"owner_id": OTHER_ORCID}).json()["items"] == []


def test_instrument_owner_filter_accepts_the_orcid(client, admin, person):
    created = client.post("/instruments", json={"instrument_name": "Scope", "instrument_id": "scope-1", "location": "Lab", "owner": ORCID})
    assert created.status_code in (200, 201), created.text
    assert created.json()["owner_orcid"] == MFID
    for param in ("owner_id", "owner"):
        items = client.get("/instruments", params={param: ORCID}).json()["items"]
        assert [item["owner_orcid"] for item in items] == [MFID]


@pytest.mark.parametrize("path", ["/datasets/facets", "/samples/facets"])
def test_facets_owner_filter_accepts_the_orcid(client, admin, db_engine, person, path):
    seed_records(db_engine)
    by_orcid = client.get(path, params={"field": "owner", "owner_id": ORCID})
    by_id = client.get(path, params={"field": "owner", "owner_id": MFID})
    assert by_orcid.status_code == 200, by_orcid.text
    assert by_orcid.json() == by_id.json()
    assert by_id.json() != client.get(path, params={"field": "owner", "owner_id": OTHER_ORCID}).json()
