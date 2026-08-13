from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Dataset, Project, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import Role


def _user(session, orcid):
    u = User(first_name="T", last_name=orcid[-4:], email=f"{orcid}@x.com", unique_id=orcid)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _group(session, name):
    ag = AccessGroup(group_name=name)
    session.add(ag)
    session.commit()
    session.refresh(ag)
    return ag


def _setup(db_engine):
    with Session(db_engine) as session:
        caller = _user(session, "0000-0031")
        proj_old = Project(project_id="proj_old", organization="LBNL", project_lead_orcid="0000-0031")
        proj_new = Project(project_id="proj_new", organization="LBNL", project_lead_orcid="0000-0031")
        session.add(proj_old)
        session.add(proj_new)
        session.commit()
        session.refresh(proj_old)
        session.refresh(proj_new)

        old_proj_ag = _group(session, proj_old.unique_id)
        new_proj_ag = _group(session, proj_new.unique_id)
        session.add(Resource(mfid=proj_old.unique_id, resource_type="project"))
        session.add(Resource(mfid=proj_new.unique_id, resource_type="project"))
        session.add(Resource(mfid="ds_1", resource_type="dataset"))
        session.add(Dataset(unique_id="ds_1", dataset_name="d", owner_orcid="0000-0031", project_id="proj_old"))
        session.commit()
        session.add(UserAccessGroupLink(user_id=caller.id, accessgroup_id=old_proj_ag.id, role="admin"))
        session.add(UserAccessGroupLink(user_id=caller.id, accessgroup_id=new_proj_ag.id, role="contributor"))
        session.add(ResourceACL(mfid="ds_1", accessgroup_id=old_proj_ag.id, read=True, write=True, role="admin"))
        # A project's own self-referential ACL entry — its own group, at owner.
        session.add(ResourceACL(mfid=proj_old.unique_id, accessgroup_id=old_proj_ag.id, read=True, write=True, role="owner"))
        session.add(ResourceACL(mfid=proj_new.unique_id, accessgroup_id=new_proj_ag.id, read=True, write=True, role="owner"))
        session.commit()
        return old_proj_ag.id, new_proj_ag.id


def test_preview_does_not_mutate_anything(client, db_engine, auth_as):
    old_ag_id, new_ag_id = _setup(db_engine)
    auth_as("0000-0031", groups=[("proj_old", old_ag_id, Role.ADMIN), ("proj_new", new_ag_id, Role.CONTRIBUTOR)])

    resp = client.post("/resources/ds_1/project", json={"project_id": "proj_new"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["previous_project_id"] == "proj_old"
    assert body["new_project_id"] == "proj_new"

    with Session(db_engine) as session:
        acls = {a.accessgroup_id for a in session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds_1")).all()}
        assert acls == {old_ag_id}
        ds = session.exec(select(Dataset).where(Dataset.unique_id == "ds_1")).one()
        assert ds.project_id == "proj_old"


def test_confirm_moves_the_acl_row_and_syncs_project_id(client, db_engine, auth_as):
    old_ag_id, new_ag_id = _setup(db_engine)
    auth_as("0000-0031", groups=[("proj_old", old_ag_id, Role.ADMIN), ("proj_new", new_ag_id, Role.CONTRIBUTOR)])

    resp = client.post("/resources/ds_1/project?confirm=true", json={"project_id": "proj_new"})
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        acls = {a.accessgroup_id: a.role for a in
               session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds_1")).all()}
        assert old_ag_id not in acls
        assert acls[new_ag_id] == "admin"

        ds = session.exec(select(Dataset).where(Dataset.unique_id == "ds_1")).one()
        assert ds.project_id == "proj_new"


def test_confirm_updates_project_mfid_too(client, db_engine, auth_as):
    old_ag_id, new_ag_id = _setup(db_engine)
    auth_as("0000-0031", groups=[("proj_old", old_ag_id, Role.ADMIN), ("proj_new", new_ag_id, Role.CONTRIBUTOR)])

    resp = client.post("/resources/ds_1/project?confirm=true", json={"project_id": "proj_new"})
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == "ds_1")).one()
        proj_new = session.exec(select(Project).where(Project.project_id == "proj_new")).one()
        assert ds.project_mfid == proj_new.unique_id


def test_requires_admin_on_resource(client, db_engine, auth_as):
    old_ag_id, new_ag_id = _setup(db_engine)
    auth_as("0000-0031", groups=[("proj_old", old_ag_id, Role.EDITOR), ("proj_new", new_ag_id, Role.CONTRIBUTOR)])

    resp = client.post("/resources/ds_1/project?confirm=true", json={"project_id": "proj_new"})
    assert resp.status_code == 403


def test_requires_contributor_in_target_project(client, db_engine, auth_as):
    old_ag_id, new_ag_id = _setup(db_engine)
    auth_as("0000-0031", groups=[("proj_old", old_ag_id, Role.ADMIN), ("proj_new", new_ag_id, Role.VIEWER)])

    resp = client.post("/resources/ds_1/project?confirm=true", json={"project_id": "proj_new"})
    assert resp.status_code == 403


def test_unknown_target_project_404s(client, db_engine, auth_as):
    old_ag_id, new_ag_id = _setup(db_engine)
    auth_as("0000-0031", groups=[("proj_old", old_ag_id, Role.ADMIN), ("proj_new", new_ag_id, Role.CONTRIBUTOR)])

    resp = client.post("/resources/ds_1/project?confirm=true", json={"project_id": "does_not_exist"})
    assert resp.status_code == 404
