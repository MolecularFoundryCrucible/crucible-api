"""POST /projects must create the project's own Resource/idtype row and its
single ACL entry (own group at owner) — before this fix, create_project wrote
no Resource row at all, so a project's creation_time/modification_time were
always null and it couldn't carry an ACL entry the way every other resource
type can. See docs/permission_system_plan.md, Core Concepts -> Projects
become resources.
"""
from sqlmodel import select

from app.tables import AccessGroup, Project, Resource, ResourceACL, User
from app.utils.permissions import PlatformRole


def test_create_project_creates_resource_and_owner_acl(client, db_engine, auth_as):
    from sqlmodel import Session
    with Session(db_engine) as session:
        session.add(User(first_name="A", last_name="Lead", email="lead@x.com", unique_id="0000-0001"))
        session.commit()

    auth_as("0000-0001", platform_role=PlatformRole.NONE)
    resp = client.post("/projects", json={
        "project_id": "proj_new", "organization": "LBNL", "project_lead_orcid": "0000-0001",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["capabilities"] == {
        "can_edit": True,
        "can_manage_access": True,
        "can_view_access": True,
        "can_change_status": True,
        "can_transfer": True,
        "can_delete": True,
        "can_request_deletion": True,
        "max_grant_role": "admin",
    }

    with Session(db_engine) as session:
        proj = session.exec(select(Project).where(Project.project_id == "proj_new")).one()

        resource = session.exec(select(Resource).where(Resource.mfid == proj.unique_id)).one_or_none()
        assert resource is not None
        assert resource.resource_type == "project"

        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == proj.unique_id)).one()
        acl_rows = session.exec(select(ResourceACL).where(ResourceACL.mfid == proj.unique_id)).all()
        assert len(acl_rows) == 1
        assert acl_rows[0].accessgroup_id == ag.id
        assert acl_rows[0].permission == "owner"
