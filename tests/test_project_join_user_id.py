
from sqlmodel import Session, select

from app.tables import AccessGroup, AccessGroupJoinRequest, Project, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PlatformRole

LEAD = "0000-0001-1111-1111"
REQUESTER = "0000-0002-2222-2222"
ADMIN = "0tmnyhfztdwc1000w2kspzsnh7"
NEW_LEAD = "0tmnyhfztdwc1000w2kspzsnh8"
NEW_REQUESTER = "0tmnyhfztdwc1000w2kspzsnh9"


def _setup(db_engine):
    with Session(db_engine) as session:
        lead, requester, admin = (User(first_name=n, last_name="X", email=None, unique_id=u, username=n.lower()) for n, u in (("Lead", LEAD), ("Req", REQUESTER), ("Admin", ADMIN)))
        session.add_all([lead, requester, admin])
        session.flush()
        project = Project(project_id="proj_x", organization="LBNL", project_lead_orcid=LEAD, lead_user_id=lead.id)
        session.add(project)
        session.flush()
        group = AccessGroup(group_name=project.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add(group)
        session.flush()
        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=group.id, role="owner"))
        session.add(ResourceACL(mfid=project.unique_id, accessgroup_id=group.id, read=True, write=True, permission="owner"))
        session.commit()
        return project.unique_id


def _rename(db_engine, old, new):
    with Session(db_engine) as session, session.begin():
        session.exec(select(User).where(User.unique_id == old)).one().unique_id = new


def test_join_request_follows_the_people_when_their_unique_id_changes(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(REQUESTER, platform_role=PlatformRole.NONE)
    request_id = client.post("/projects/proj_x/join_requests", json={}).json()["id"]
    auth_as(ADMIN, groups=[], platform_role=PlatformRole.ADMIN)
    assert client.patch(f"/join_requests/{request_id}", json={"status": "rejected"}).status_code == 200
    _rename(db_engine, REQUESTER, NEW_REQUESTER)
    _rename(db_engine, ADMIN, "0tmnyhfztdwc1000w2kspzsnh6")

    auth_as(NEW_REQUESTER, platform_role=PlatformRole.NONE)
    mine = client.get("/join_requests", params={"scope": "submitted"}).json()["items"]
    assert [(item["id"], item["requester_id"], item["reviewer_id"]) for item in mine] == [(request_id, NEW_REQUESTER, "0tmnyhfztdwc1000w2kspzsnh6")]
    assert client.get("/join_requests", params={"requester_id": NEW_REQUESTER}).json()["total"] == 1
    assert client.get("/join_requests", params={"requester_id": REQUESTER}).json()["total"] == 0
    with Session(db_engine) as session:
        row = session.get(AccessGroupJoinRequest, request_id)
        ids = {u.unique_id: u.id for u in session.exec(select(User)).all()}
        assert (row.requester_user_id, row.reviewer_user_id) == (ids[NEW_REQUESTER], ids["0tmnyhfztdwc1000w2kspzsnh6"])


def test_withdraw_checks_the_requester_by_internal_id(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(REQUESTER, platform_role=PlatformRole.NONE)
    request_id = client.post("/projects/proj_x/join_requests", json={}).json()["id"]
    _rename(db_engine, REQUESTER, NEW_REQUESTER)
    auth_as(NEW_REQUESTER, platform_role=PlatformRole.NONE)
    response = client.post(f"/join_requests/{request_id}/withdraw")
    assert response.status_code == 200, response.text
    assert response.json()["capabilities"]["can_withdraw"] is False


def test_project_lead_is_read_from_the_lead_user(client, db_engine, auth_as):
    mfid = _setup(db_engine)
    _rename(db_engine, LEAD, NEW_LEAD)
    auth_as(REQUESTER, platform_role=PlatformRole.NONE)
    detail = client.get(f"/projects/{mfid}").json()
    assert (detail["project_lead_orcid"], detail["lead"]["unique_id"]) == (NEW_LEAD, NEW_LEAD)
    listed = client.get("/projects", params={"project_id": "proj_x"}).json()["items"]
    assert [item["project_lead_orcid"] for item in listed] == [NEW_LEAD]


def test_adding_a_requester_directly_closes_their_request_by_internal_id(client, db_engine, auth_as):
    mfid = _setup(db_engine)
    auth_as(REQUESTER, platform_role=PlatformRole.NONE)
    request_id = client.post("/projects/proj_x/join_requests", json={}).json()["id"]
    _rename(db_engine, REQUESTER, NEW_REQUESTER)
    auth_as(ADMIN, groups=[], platform_role=PlatformRole.ADMIN)
    response = client.post(f"/projects/{mfid}/users/{NEW_REQUESTER}")
    assert response.status_code == 200, response.text
    with Session(db_engine) as session:
        row = session.get(AccessGroupJoinRequest, request_id)
        admin_id = session.exec(select(User.id).where(User.unique_id == ADMIN)).one()
        assert (row.status, row.reviewer_user_id, row.reviewer_id) == ("approved", admin_id, ADMIN)
