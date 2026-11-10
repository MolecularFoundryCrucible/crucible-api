"""GET /users/{user_id}/projects: the user themselves and callers with
platform_role contributor or admin. Instrument-group membership grants nothing.
"""
from sqlmodel import Session

from app.tables import AccessGroup, Instrument, Project, User, UserAccessGroupLink
from app.utils.permissions import PlatformRole, Role


def _user(session, orcid):
    u = User(first_name="T", last_name=orcid[-4:], email=f"{orcid}@x.com", unique_id=orcid)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _setup(db_engine):
    with Session(db_engine) as session:
        target = _user(session, "0000-0001")
        proj = Project(project_id="proj_a", organization="LBNL", project_lead_orcid="0000-0001", title="Alpha")
        session.add(proj)
        session.commit()
        session.refresh(proj)
        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        session.commit()
        session.refresh(ag)
        session.add(UserAccessGroupLink(user_id=target.id, accessgroup_id=ag.id, role="contributor"))
        session.add(Instrument(unique_id="inst-hip-1", instrument_name="hip_microscope", owner="lab", location="b30"))
        session.commit()


def _assert_full_details(resp):
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["project_id"] == "proj_a"
    assert item["title"] == "Alpha"
    assert "lead" in item


def test_self_gets_full_details(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", groups=[])
    _assert_full_details(client.get("/users/0000-0001/projects"))


def test_platform_admin_gets_full_details(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("someone-else", groups=[], platform_role=PlatformRole.ADMIN)
    _assert_full_details(client.get("/users/0000-0001/projects"))


def test_platform_contributor_gets_full_details(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0003", groups=[], platform_role=PlatformRole.CONTRIBUTOR)
    _assert_full_details(client.get("/users/0000-0001/projects"))


def test_unbound_contributor_service_account_gets_full_details(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0sa0000000000000000000000a", groups=[], platform_role=PlatformRole.CONTRIBUTOR, is_service_account=True)
    _assert_full_details(client.get("/users/0000-0001/projects"))


def test_instrument_group_membership_alone_is_rejected(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("sa-instrument-1", groups=[("inst-hip-1", 999, Role.OWNER)], is_service_account=True)
    assert client.get("/users/0000-0001/projects").status_code == 403


def test_unrelated_caller_is_rejected(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002", groups=[])
    assert client.get("/users/0000-0001/projects").status_code == 403
