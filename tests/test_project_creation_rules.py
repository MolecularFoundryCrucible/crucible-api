import pytest
from sqlmodel import Session, select

from app.config import get_settings

from app.tables import AccessGroup, Project, User
from app.utils.permissions import PlatformRole

ME, OTHER, ROBOT = "0000-0002-1825-0097", "0000-0001-5109-3700", "0tmnyhfztdwc1000w2kspzsnh9"


@pytest.fixture
def people(db_engine):
    with Session(db_engine) as session, session.begin():
        session.add(User(first_name="A", last_name="Me", email="me@x.com", unique_id=ME))
        session.add(User(first_name="B", last_name="Other", email="other@x.com", unique_id=OTHER))
        session.add(User(first_name="R", last_name="Robot", unique_id=ROBOT, is_service_account=True))


def create(client, lead, project_id="proj_new"):
    return client.post("/projects", json={"project_id": project_id, "organization": "LBNL", "project_lead": lead})


def stored(db_engine, project_id="proj_new"):
    with Session(db_engine) as session:
        return session.exec(select(Project).where(Project.project_id == project_id)).one_or_none()


def test_a_person_creates_their_own_project_without_proposal_sync(client, db_engine, auth_as, people):
    auth_as(ME)
    response = create(client, ME)
    assert response.status_code == 200, response.text
    assert response.json()["proposal_sync"] is False
    assert stored(db_engine).proposal_sync is False


def test_a_person_cannot_create_a_project_led_by_someone_else(client, db_engine, auth_as, people):
    auth_as(ME)
    refused = create(client, OTHER)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "lead_must_be_caller"
    assert stored(db_engine) is None


def test_an_administrator_creates_a_project_for_someone_else_with_proposal_sync(client, db_engine, auth_as, people):
    auth_as(ME, platform_role=PlatformRole.ADMIN)
    response = create(client, OTHER)
    assert response.status_code == 200, response.text
    assert response.json()["proposal_sync"] is True
    assert stored(db_engine).proposal_sync is True


@pytest.mark.parametrize("caller, role, service_account", [(ME, PlatformRole.ADMIN, False), (ROBOT, PlatformRole.NONE, True)])
def test_a_service_account_cannot_lead_a_project(client, db_engine, auth_as, people, caller, role, service_account):
    auth_as(caller, platform_role=role, is_service_account=service_account)
    assert create(client, ROBOT).status_code == 422
    assert stored(db_engine) is None


def test_only_an_administrator_sets_proposal_sync(client, db_engine, auth_as, people):
    auth_as(ME)
    create(client, ME)
    assert client.patch("/projects/proj_new", json={"proposal_sync": True}).status_code == 403
    assert stored(db_engine).proposal_sync is False
    auth_as(OTHER, platform_role=PlatformRole.ADMIN)
    response = client.patch("/projects/proj_new", json={"proposal_sync": True})
    assert response.status_code == 200, response.text
    assert response.json()["proposal_sync"] is True


def test_a_rename_by_the_lead_turns_proposal_sync_off_and_one_by_an_administrator_keeps_it(client, db_engine, auth_as, people):
    auth_as(OTHER, platform_role=PlatformRole.ADMIN)
    create(client, ME)
    assert client.patch("/projects/proj_new", json={"project_id": "proj_admin"}).json()["proposal_sync"] is True
    auth_as(ME)
    response = client.patch("/projects/proj_admin", json={"project_id": "MF-0001"})
    assert response.status_code == 200, response.text
    assert response.json()["proposal_sync"] is False
    assert stored(db_engine, "MF-0001").proposal_sync is False


@pytest.fixture
def reserved(monkeypatch):
    monkeypatch.setenv("RESERVED_PROJECT_ID_PATTERN", r"MFP\d{5}")
    get_settings.cache_clear()


@pytest.mark.parametrize("project_id", ["MFP12345", "mfp12345"])
def test_only_an_administrator_creates_a_project_with_a_reserved_id(client, db_engine, auth_as, people, reserved, project_id):
    auth_as(ME)
    refused = create(client, ME, project_id)
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "project_id_reserved"
    assert stored(db_engine, project_id) is None
    assert create(client, ME, "MFP1234").status_code == 200
    assert create(client, ME, "xMFP12345").status_code == 200
    auth_as(OTHER, platform_role=PlatformRole.ADMIN)
    assert create(client, ME, project_id).status_code == 200


def test_only_an_administrator_renames_to_or_from_a_reserved_id(client, db_engine, auth_as, people, reserved):
    auth_as(ME)
    create(client, ME, "mine")
    auth_as(OTHER, platform_role=PlatformRole.ADMIN)
    create(client, ME, "MFP00001")
    with Session(db_engine) as session:
        groups = [(name, group_id) for name, group_id in session.exec(select(AccessGroup.group_name, AccessGroup.id)).all()]
    auth_as(ME, groups=groups)
    to_reserved = client.patch("/projects/mine", json={"project_id": "MFP00002"})
    from_reserved = client.patch("/projects/MFP00001", json={"project_id": "taken-away"})
    assert (to_reserved.status_code, from_reserved.status_code) == (403, 403)
    assert to_reserved.json()["detail"]["code"] == from_reserved.json()["detail"]["code"] == "project_id_reserved"
    assert client.patch("/projects/MFP00001", json={"title": "A title"}).status_code == 200
    assert stored(db_engine, "MFP00001").proposal_sync is True
    auth_as(OTHER, platform_role=PlatformRole.ADMIN)
    assert client.patch("/projects/MFP00001", json={"project_id": "MFP00003"}).status_code == 200


def test_no_id_is_reserved_without_the_setting(client, auth_as, people):
    auth_as(ME)
    assert create(client, ME, "MFP12345").status_code == 200


@pytest.mark.parametrize("value, expected", [("", None), ("  ", None), (r"MFP\d{5}", r"MFP\d{5}")])
def test_the_pattern_setting_is_read(monkeypatch, value, expected):
    monkeypatch.setenv("RESERVED_PROJECT_ID_PATTERN", value)
    get_settings.cache_clear()
    assert get_settings().api.reserved_project_id_pattern == expected


def test_an_invalid_pattern_is_refused(monkeypatch):
    monkeypatch.setenv("RESERVED_PROJECT_ID_PATTERN", "MFP(")
    get_settings.cache_clear()
    with pytest.raises(Exception, match="RESERVED_PROJECT_ID_PATTERN|regular expression"):
        get_settings().api

