"""GET /users/{orcid}/projects: self and platform admin get full details;
a caller with standing in any instrument group gets project_id + title only,
for rendering a project picker — nothing wider (no lead, no metadata).
"""
from sqlmodel import Session

from app.models.core_models import AccessGroup, Instrument, Project, User, UserAccessGroupLink
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


def test_self_gets_full_details(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001", groups=[])

    resp = client.get("/users/0000-0001/projects")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item["project_id"] == "proj_a"
    assert "lead" in item


def test_platform_admin_gets_full_details(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("someone-else", groups=[], platform_role=PlatformRole.ADMIN)

    resp = client.get("/users/0000-0001/projects")
    assert resp.status_code == 200, resp.text
    assert "lead" in resp.json()["items"][0]


def test_instrument_group_member_gets_minimal_projection(client, db_engine, auth_as):
    auth_as("sa-instrument-1", groups=[("inst-hip-1", 999, Role.OWNER)])
    _setup(db_engine)

    resp = client.get("/users/0000-0001/projects")
    assert resp.status_code == 200, resp.text
    item = resp.json()["items"][0]
    assert item == {"project_id": "proj_a", "title": "Alpha"}


def test_unrelated_caller_is_rejected(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002", groups=[])

    resp = client.get("/users/0000-0001/projects")
    assert resp.status_code == 403
