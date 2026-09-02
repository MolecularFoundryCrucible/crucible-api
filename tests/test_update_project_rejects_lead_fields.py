"""PATCH /projects/{proj_id} must reject project_lead_orcid/email/username.

Before this fix it accepted them as a bare field edit, gated at editor (not
owner) and never touching the actual owner standing — the same
"field-edit-instead-of-a-real-transition" bug already known for owner_orcid
on datasets/samples. Leadership now only changes through
POST /resources/{proj_id}/transfer_ownership.

Enforced at the schema layer: ProjectUpdate has no project_lead_* fields and
sets extra="forbid", so these 422 before the route body ever runs, rather
than needing a runtime check.
"""
from sqlmodel import Session

from app.models.core_models import AccessGroup, Project, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import Role


def _setup(db_engine):
    with Session(db_engine) as session:
        lead = User(first_name="A", last_name="Lead", email="lead@x.com", unique_id="0000-0001")
        session.add(lead)
        proj = Project(project_id="proj_x", organization="LBNL", project_lead_orcid="0000-0001")
        session.add(proj)
        session.commit()
        session.refresh(proj)
        mfid = proj.unique_id
        ag = AccessGroup(group_name=mfid)
        session.add(ag)
        session.add(Resource(mfid=mfid, resource_type="project"))
        session.commit()
        session.refresh(ag)
        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=ag.id, role="owner"))
        session.add(ResourceACL(mfid=mfid, accessgroup_id=ag.id, read=True, write=True, permission="owner"))
        session.commit()
        return ag.id


def test_patch_rejects_project_lead_orcid(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001", groups=[("proj_x", ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_x", json={"project_lead_orcid": "0000-0002"})
    assert resp.status_code == 422
    assert "project_lead_orcid" in resp.text


def test_patch_rejects_project_lead_email(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001", groups=[("proj_x", ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_x", json={"project_lead_email": "someone@x.com"})
    assert resp.status_code == 422


def test_patch_still_allows_ordinary_fields(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001", groups=[("proj_x", ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_x", json={"title": "New Title"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["title"] == "New Title"
