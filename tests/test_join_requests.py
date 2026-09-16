"""POST /access_groups/{group_name}/join and the join-request review flow.

No test previously exercised these routes end to end, which is how a real
bug shipped unnoticed: AccessGroupJoinRequest.group_name FK'd to
accessgroup.group_name, but since the C4b rename that column holds a
project's MFID (Project.unique_id), not its project_id — the value these
routes actually write. Every insert here should have violated the FK on a
real (constraint-enforcing) database. Fixed by switching the FK to
accessgroup_id (an internal id, no label/MFID ambiguity to get wrong).
"""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, AccessGroupJoinRequest, Project, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PlatformRole, Role


def _setup(db_engine, lead_orcid="0000-0001", member_orcid="0000-0002"):
    with Session(db_engine) as session:
        lead = User(first_name="A", last_name="Lead", email="lead@x.com", unique_id=lead_orcid)
        requester = User(first_name="B", last_name="Req", email="req@x.com", unique_id=member_orcid)
        session.add(lead)
        session.add(requester)
        proj = Project(project_id="proj_x", organization="LBNL", project_lead_orcid=lead_orcid)
        session.add(proj)
        session.commit()
        session.refresh(proj)
        session.refresh(requester)

        ag = AccessGroup(group_name=proj.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add(ag)
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=ag.id, role="owner"))
        session.commit()
        return ag.id, requester.id


def test_join_request_stores_accessgroup_id_not_a_label(client, db_engine, auth_as):
    ag_id, _ = _setup(db_engine)
    auth_as("0000-0002", platform_role=PlatformRole.NONE)

    resp = client.post("/access_groups/proj_x/join", json={"reason": "need access"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["group_name"] == "proj_x"

    with Session(db_engine) as session:
        req = session.exec(select(AccessGroupJoinRequest)).one()
        assert req.accessgroup_id == ag_id
        assert req.status == "pending"


def test_duplicate_pending_request_is_rejected(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002", platform_role=PlatformRole.NONE)

    resp1 = client.post("/access_groups/proj_x/join", json={})
    assert resp1.status_code == 200, resp1.text
    resp2 = client.post("/access_groups/proj_x/join", json={})
    assert resp2.status_code == 409


def test_duplicate_pending_request_is_rejected_by_database(db_engine):
    ag_id, _ = _setup(db_engine)

    with Session(db_engine) as session:
        session.add(AccessGroupJoinRequest(
            accessgroup_id=ag_id,
            requester_id="0000-0002",
            status="pending",
        ))
        session.commit()
        session.add(AccessGroupJoinRequest(
            accessgroup_id=ag_id,
            requester_id="0000-0002",
            status="pending",
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_new_pending_request_is_allowed_after_rejection(db_engine):
    ag_id, _ = _setup(db_engine)

    with Session(db_engine) as session:
        session.add(AccessGroupJoinRequest(
            accessgroup_id=ag_id,
            requester_id="0000-0002",
            status="rejected",
        ))
        session.add(AccessGroupJoinRequest(
            accessgroup_id=ag_id,
            requester_id="0000-0002",
            status="pending",
        ))
        session.commit()


def test_unknown_project_404s(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002", platform_role=PlatformRole.NONE)

    resp = client.post("/access_groups/does-not-exist/join", json={})
    assert resp.status_code == 404


def test_lead_can_list_and_approve_join_request(client, db_engine, auth_as):
    ag_id, requester_id = _setup(db_engine)
    auth_as("0000-0002", platform_role=PlatformRole.NONE)
    resp = client.post("/access_groups/proj_x/join", json={"reason": "please"})
    assert resp.status_code == 200, resp.text
    request_id = resp.json()["id"]

    auth_as("0000-0001", groups=[("proj_x", ag_id, Role.OWNER)])
    listed = client.get("/join_requests", params={"group_name": "proj_x"})
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] == 1
    assert listed.json()["items"][0]["group_name"] == "proj_x"

    resp = client.patch(f"/join_requests/{request_id}", json={"status": "approved"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"

    with Session(db_engine) as session:
        link = session.exec(
            select(UserAccessGroupLink).where(UserAccessGroupLink.accessgroup_id == ag_id,
                                              UserAccessGroupLink.user_id == requester_id)
        ).one_or_none()
        assert link is not None

    repeated = client.patch(f"/join_requests/{request_id}", json={"status": "approved"})
    assert repeated.status_code == 409


def test_non_lead_cannot_review(client, db_engine, auth_as):
    ag_id, _ = _setup(db_engine)
    auth_as("0000-0002", platform_role=PlatformRole.NONE)
    resp = client.post("/access_groups/proj_x/join", json={})
    request_id = resp.json()["id"]

    auth_as("0000-0002", platform_role=PlatformRole.NONE)
    resp = client.patch(f"/join_requests/{request_id}", json={"status": "approved"})
    assert resp.status_code == 403


def test_service_account_cannot_use_project_lead_standing(client, db_engine, auth_as):
    ag_id, _ = _setup(db_engine)
    auth_as("0000-0002", platform_role=PlatformRole.NONE)
    created = client.post("/access_groups/proj_x/join", json={})
    request_id = created.json()["id"]

    auth_as(
        "0000-0001",
        groups=[("proj_x", ag_id, Role.OWNER)],
        is_service_account=True,
    )
    listed = client.get("/join_requests", params={"group_name": "proj_x"})
    reviewed = client.patch(f"/join_requests/{request_id}", json={"status": "approved"})

    assert listed.status_code == 403
    assert reviewed.status_code == 403


def test_platform_role_admin_can_list_and_review(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002", platform_role=PlatformRole.NONE)
    created = client.post("/access_groups/proj_x/join", json={})
    request_id = created.json()["id"]

    auth_as("platform-admin", groups=[], platform_role=PlatformRole.ADMIN)
    listed = client.get("/join_requests")
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    reviewed = client.patch(f"/join_requests/{request_id}", json={"status": "approved"})
    assert reviewed.status_code == 200
