"""GET /projects/{proj_id}?include_members= -- members-only, public-safe
(no email) member list enriched with role. Also covers the is_member bug
this uncovered: it compared the caller's group_name against proj_id (the
mutable label), but project groups have been keyed by Project.unique_id
(the MFID) since the C4b rename, so a real member's own group_name never
matched -- is_member was effectively always False for real members, only
ever True for the literal 'admin' bypass group. Fixed by comparing against
the project's own unique_id instead.
"""
from sqlmodel import Session

from app.models.core_models import AccessGroup, Project, User, UserAccessGroupLink
from app.utils.permissions import PlatformRole, Role


def _setup(db_engine):
    with Session(db_engine) as session:
        lead = User(first_name="A", last_name="Lead", email="lead@x.com", unique_id="0000-0001-1111-1111")
        member = User(first_name="B", last_name="Member", email="member@x.com", unique_id="0000-0002-2222-2222")
        outsider = User(first_name="C", last_name="Outsider", email="outsider@x.com", unique_id="0000-0003-3333-3333")
        session.add(lead)
        session.add(member)
        session.add(outsider)
        session.flush()
        proj = Project(project_id="proj_x", organization="LBNL", project_lead_orcid=lead.unique_id, lead_user_id=lead.id)
        session.add(proj)
        session.commit()
        session.refresh(proj)
        session.refresh(lead)
        session.refresh(member)

        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=ag.id, role="owner"))
        session.add(UserAccessGroupLink(user_id=member.id, accessgroup_id=ag.id, role="contributor"))
        session.commit()
        return ag.id, proj.unique_id


def test_member_sees_public_safe_member_list(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[(mfid, ag_id, Role.CONTRIBUTOR)])

    resp = client.get("/projects/proj_x", params={"include_members": True})
    assert resp.status_code == 200, resp.text
    members = resp.json()["members"]
    assert members is not None
    by_id = {m["unique_id"]: m for m in members}
    assert set(by_id) == {"0000-0001-1111-1111", "0000-0002-2222-2222"}
    assert by_id["0000-0001-1111-1111"]["role"] == "owner"
    assert by_id["0000-0002-2222-2222"]["role"] == "contributor"
    assert "email" not in by_id["0000-0001-1111-1111"]


def test_non_member_gets_no_member_list(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0003-3333-3333", platform_role=PlatformRole.NONE)

    resp = client.get("/projects/proj_x", params={"include_members": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["members"] is None


def test_member_list_omitted_without_the_flag(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[(mfid, ag_id, Role.CONTRIBUTOR)])

    resp = client.get("/projects/proj_x")
    assert resp.status_code == 200, resp.text
    assert resp.json()["members"] is None


def test_member_sees_lead_without_email(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0002-2222-2222", groups=[(mfid, ag_id, Role.CONTRIBUTOR)])

    resp = client.get("/projects/proj_x")
    assert resp.status_code == 200, resp.text
    assert "email" not in resp.json()["lead"]


def test_outsider_sees_lead_without_email(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0003-3333-3333", platform_role=PlatformRole.NONE)

    resp = client.get("/projects/proj_x")
    assert resp.status_code == 200, resp.text
    assert "email" not in resp.json()["lead"]
