"""GET/PUT/DELETE /resources/{mfid}/access[/...] -- previously had zero test
coverage, discovered while renaming ResourceACL.role -> permission and
AccessGrantRead/Write.role -> effective_permission. Locks in the new field
name end to end, not just at the model/ORM layer.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Project, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import PUBLIC_GROUP_NAME, Role


def _setup(db_engine):
    with Session(db_engine) as session:
        owner = User(first_name="A", last_name="Owner", email="a@x.com", unique_id="0000-0001-2345-6789")
        grantee = User(first_name="B", last_name="Grantee", email="b@x.com", unique_id="0000-0002-3456-7890")
        session.add(owner)
        session.add(grantee)
        session.commit()
        session.refresh(owner)
        session.refresh(grantee)

        owner_ag = AccessGroup(group_name="0000-0001-2345-6789")
        grantee_ag = AccessGroup(group_name="0000-0002-3456-7890")
        public_ag = AccessGroup(group_name=PUBLIC_GROUP_NAME)
        session.add(owner_ag)
        session.add(grantee_ag)
        session.add(public_ag)
        session.add(Resource(mfid="ds_1", resource_type="dataset"))
        session.commit()
        session.refresh(owner_ag)
        session.refresh(grantee_ag)

        session.add(UserAccessGroupLink(user_id=owner.id, accessgroup_id=owner_ag.id, role="owner"))
        session.add(ResourceACL(mfid="ds_1", accessgroup_id=owner_ag.id, read=True, write=True,
                                permission=Role.OWNER.db_value))
        session.commit()
        return owner_ag.id


def test_list_resource_access_shows_effective_permission(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.get("/resources/ds_1/access")
    assert resp.status_code == 200, resp.text
    grants = resp.json()
    assert len(grants) == 1
    assert grants[0]["effective_permission"] == "owner"
    assert grants[0]["principal"] == "0000-0001-2345-6789"


def test_set_resource_access_grants_and_returns_effective_permission(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.put("/resources/ds_1/access/users/0000-0002-3456-7890",
                      json={"effective_permission": "editor"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective_permission"] == "editor"
    assert resp.json()["principal"] == "0000-0002-3456-7890"

    with Session(db_engine) as session:
        acl = session.exec(
            select(ResourceACL).where(ResourceACL.mfid == "ds_1",
                                      ResourceACL.accessgroup_id != ag_id)
        ).one()
        assert acl.permission == "editor"


def test_set_resource_access_rejects_unknown_permission_value(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.put("/resources/ds_1/access/users/0000-0002-3456-7890",
                      json={"effective_permission": "not-a-role"})
    assert resp.status_code == 422
    assert "effective_permission" in resp.text


def test_revoke_resource_access(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])
    client.put("/resources/ds_1/access/users/0000-0002-3456-7890", json={"effective_permission": "editor"})

    resp = client.delete("/resources/ds_1/access/users/0000-0002-3456-7890")
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        remaining = session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds_1")).all()
        assert len(remaining) == 1  # only the owner's grant is left


def test_revoke_owner_access_is_refused(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.delete("/resources/ds_1/access/users/0000-0001-2345-6789")
    assert resp.status_code == 409


def test_publish_and_unpublish_resource(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.put("/resources/ds_1/access/public")
    assert resp.status_code == 200, resp.text
    assert resp.json()["effective_permission"] == "viewer"
    assert resp.json()["kind"] == "public"

    with Session(db_engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == "ds_1")).one()
        assert resource.public is True

    resp = client.delete("/resources/ds_1/access/public")
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == "ds_1")).one()
        assert resource.public is False
