"""A project has exactly one owner, changed only via
POST /resources/{mfid}/transfer_ownership -- POST/PATCH
/projects/{proj_id}/users/{orcid} must refuse to appoint or touch owner
standing, even when the caller is the current owner. Before this fix, an
owner could grant a second member OWNER via either route (can_grant only
blocks granting *above* your own role, and OWNER isn't above OWNER),
silently desyncing Project.project_lead_orcid from who actually holds
owner standing.
"""
from sqlmodel import Session, select

from app.tables import AccessGroup, Project, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import Role


def _setup(db_engine):
    with Session(db_engine) as session:
        lead = User(first_name="A", last_name="Lead", email="lead@x.com", unique_id="0000-0001-1111-1111")
        member = User(first_name="B", last_name="Member", email="member@x.com", unique_id="0000-0002-2222-2222")
        newcomer = User(first_name="C", last_name="New", email="new@x.com", unique_id="0000-0003-3333-3333")
        session.add(lead)
        session.add(member)
        session.add(newcomer)
        proj = Project(project_id="proj_x", organization="LBNL", project_lead_orcid=lead.unique_id)
        session.add(proj)
        session.commit()
        session.refresh(proj)
        session.refresh(lead)
        session.refresh(member)

        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        session.add(Resource(mfid=proj.unique_id, resource_type="project"))
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=ag.id, role="owner"))
        session.add(UserAccessGroupLink(user_id=member.id, accessgroup_id=ag.id, role="contributor"))
        session.add(ResourceACL(mfid=proj.unique_id, accessgroup_id=ag.id, read=True, write=True,
                                permission="owner"))
        session.commit()
        return ag.id, proj.unique_id


def test_cannot_appoint_a_new_owner_via_post(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(mfid, ag_id, Role.OWNER)])

    resp = client.post("/projects/proj_x/users/0000-0003-3333-3333", params={"role": Role.OWNER.db_value})
    assert resp.status_code == 409

    with Session(db_engine) as session:
        # No membership row was created for the would-be co-owner.
        user = session.exec(select(User).where(User.unique_id == "0000-0003-3333-3333")).one()
        link = session.exec(
            select(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id)
        ).one_or_none()
        assert link is None


def test_cannot_promote_an_existing_member_to_owner_via_patch(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(mfid, ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_x/users/0000-0002-2222-2222", params={"role": Role.OWNER.db_value})
    assert resp.status_code == 409

    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == "0000-0002-2222-2222")).one()
        link = session.exec(
            select(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id)
        ).one()
        assert link.role == "contributor"  # unchanged


def test_cannot_demote_the_owner_via_patch(client, db_engine, auth_as):
    """Even the owner acting on their own entry must go through transfer,
    not a bare role edit down from owner."""
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(mfid, ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_x/users/0000-0001-1111-1111", params={"role": Role.ADMIN.db_value})
    assert resp.status_code == 409

    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == "0000-0001-1111-1111")).one()
        link = session.exec(
            select(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id)
        ).one()
        assert link.role == "owner"  # unchanged


def test_ordinary_role_changes_still_work(client, db_engine, auth_as):
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(mfid, ag_id, Role.OWNER)])

    resp = client.patch("/projects/proj_x/users/0000-0002-2222-2222", params={"role": Role.EDITOR.db_value})
    assert resp.status_code == 200, resp.text


def test_demoting_or_removing_a_member_drops_their_cached_keys(client, db_engine, auth_as, monkeypatch):
    forgotten = []
    monkeypatch.setattr("app.api.routes.projects.forget_cached_keys", lambda session, user_id: forgotten.append(user_id))
    ag_id, mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111", groups=[(mfid, ag_id, Role.OWNER)])
    assert client.patch("/projects/proj_x/users/0000-0002-2222-2222", params={"role": Role.VIEWER.db_value}).status_code == 200
    assert client.delete("/projects/proj_x/users/0000-0002-2222-2222").status_code == 200
    with Session(db_engine) as session:
        member = session.exec(select(User.id).where(User.unique_id == "0000-0002-2222-2222")).one()
    assert forgotten == [member, member]
