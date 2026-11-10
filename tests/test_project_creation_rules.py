import pytest
from sqlmodel import Session, select

from app.tables import Project, User
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
