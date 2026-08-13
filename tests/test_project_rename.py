"""PATCH /projects/{proj_id} can now rename project_id itself. Safe because
every FK that names a project stores its MFID (Project.unique_id), never
project_id -- see docs/permission_system_plan.md, "Project identity: mfid vs
project_id". Dataset/Sample display resolution living up to that promise is
covered separately in test_project_rename_resolves_live.py.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Project, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import Role


def _setup(db_engine, project_id="proj_old"):
    with Session(db_engine) as session:
        lead = User(first_name="A", last_name="Lead", email="lead@x.com", unique_id="0000-0001")
        session.add(lead)
        proj = Project(project_id=project_id, organization="LBNL", project_lead_orcid="0000-0001")
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
        return ag.id, mfid


def test_rename_changes_project_id(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001", groups=[("proj_old", ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_old", json={"project_id": "proj_new"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["project_id"] == "proj_new"

    with Session(db_engine) as session:
        proj = session.exec(select(Project).where(Project.unique_id == mfid)).one()
        assert proj.project_id == "proj_new"

    # Old name no longer resolves; new one does.
    assert client.get("/projects/proj_old").status_code == 404
    assert client.get("/projects/proj_new").status_code == 200


def test_rename_does_not_move_the_acl_or_group(client, db_engine, auth_as):
    """The ACL/group identity is the MFID -- renaming project_id must not
    touch idtype.mfid/resourceacl.mfid/accessgroup.group_name at all."""
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001", groups=[("proj_old", ag_id, Role.OWNER)])

    client.patch("/projects/proj_old", json={"project_id": "proj_new"})

    with Session(db_engine) as session:
        assert session.exec(select(Resource).where(Resource.mfid == mfid)).one() is not None
        assert session.exec(select(ResourceACL).where(ResourceACL.mfid == mfid)).one() is not None
        assert session.exec(select(AccessGroup).where(AccessGroup.group_name == mfid)).one() is not None

    # The caller's standing still resolves against the (unchanged) mfid, so
    # a follow-up edit under the new name still works with the same auth.
    resp = client.patch("/projects/proj_new", json={"title": "still editable"})
    assert resp.status_code == 200, resp.text


def test_rename_to_existing_project_id_409s(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine, project_id="proj_a")
    with Session(db_engine) as session:
        session.add(Project(project_id="proj_b", organization="LBNL", project_lead_orcid="0000-0001"))
        session.commit()

    auth_as("0000-0001", groups=[("proj_a", ag_id, Role.OWNER)])
    resp = client.patch("/projects/proj_a", json={"project_id": "proj_b"})
    assert resp.status_code == 409


def test_rename_to_reserved_name_422s(client, db_engine, auth_as):
    """Reserved-word rejection happens in the same field validator
    ProjectCreate uses -- a 422 at the schema layer, not a 409 route check."""
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001", groups=[("proj_old", ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_old", json={"project_id": "admin"})
    assert resp.status_code == 422


def test_rename_rejects_malformed_project_id(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001", groups=[("proj_old", ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_old", json={"project_id": "a"})
    assert resp.status_code == 422


def test_editor_cannot_rename(client, db_engine, auth_as):
    """Rename is gated the same as any other project edit -- editor+, no
    special higher bar, since it's just another field on the record."""
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0002", groups=[("proj_old", ag_id, Role.VIEWER)])

    resp = client.patch("/projects/proj_old", json={"project_id": "proj_new"})
    assert resp.status_code == 403
