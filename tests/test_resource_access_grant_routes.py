"""GET/PUT/DELETE /resources/{mfid}/access[/...] contract coverage."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Project, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PUBLIC_GROUP_NAME, PlatformRole, Role


def _setup(db_engine):
    with Session(db_engine) as session:
        owner = User(first_name="A", last_name="Owner", email="a@x.com", unique_id="0000-0001-2345-6789")
        grantee = User(first_name="B", last_name="Grantee", email="b@x.com", unique_id="0000-0002-3456-7890")
        session.add(owner)
        session.add(grantee)
        session.commit()
        session.refresh(owner)
        session.refresh(grantee)

        owner_ag = AccessGroup(
            group_name="0000-0001-2345-6789",
            group_type=AccessGroupType.IDENTITY.value,
        )
        grantee_ag = AccessGroup(
            group_name="0000-0002-3456-7890",
            group_type=AccessGroupType.IDENTITY.value,
        )
        public_ag = AccessGroup(
            group_name=PUBLIC_GROUP_NAME,
            group_type=AccessGroupType.SYSTEM.value,
        )
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


def test_list_resource_access_shows_canonical_grant(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.get("/resources/ds_1/access")
    assert resp.status_code == 200, resp.text
    grants = resp.json()
    assert len(grants) == 1
    assert grants[0] == {
        "principal_id": "0000-0001-2345-6789",
        "principal_type": "user",
        "permission": "owner",
        "slug": None,
        "display_name": "A Owner",
    }


def test_list_resource_access_requires_manage_acl(client, db_engine, auth_as):
    _setup(db_engine)
    with Session(db_engine) as session:
        grantee_group = session.exec(
            select(AccessGroup).where(AccessGroup.group_name == "0000-0002-3456-7890")
        ).one()
        session.add(ResourceACL(
            mfid="ds_1",
            accessgroup_id=grantee_group.id,
            read=True,
            write=False,
            permission=Role.VIEWER.db_value,
        ))
        session.commit()
        grantee_group_id = grantee_group.id

    auth_as("0000-0002-3456-7890", groups=[("0000-0002-3456-7890", grantee_group_id, Role.OWNER)])
    resp = client.get("/resources/ds_1/access")
    assert resp.status_code == 403


def test_set_resource_access_grants_and_returns_permission(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.put("/resources/ds_1/access/users/0000-0002-3456-7890",
                      json={"permission": "editor"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["permission"] == "editor"
    assert resp.json()["principal_id"] == "0000-0002-3456-7890"

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
                      json={"permission": "not-a-role"})
    assert resp.status_code == 422
    assert "permission" in resp.text


def test_set_resource_access_rejects_owner_and_directs_to_transfer(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.put(
        "/resources/ds_1/access/users/0000-0002-3456-7890",
        json={"permission": "owner"},
    )

    assert resp.status_code == 409
    assert "transfer_ownership" in resp.json()["detail"]
    with Session(db_engine) as session:
        grants = session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds_1")).all()
        assert len(grants) == 1


def test_set_resource_access_refuses_to_change_existing_owner(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.put(
        "/resources/ds_1/access/users/0000-0001-2345-6789",
        json={"permission": "admin"},
    )

    assert resp.status_code == 409
    assert "transfer_ownership" in resp.json()["detail"]
    with Session(db_engine) as session:
        owner_acl = session.exec(
            select(ResourceACL).where(
                ResourceACL.mfid == "ds_1",
                ResourceACL.accessgroup_id == ag_id,
            )
        ).one()
        assert owner_acl.permission == "owner"


def test_set_resource_access_authorizes_before_owner_validation(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0002-3456-7890", groups=[("unrelated", 999, Role.VIEWER)])

    resp = client.put(
        "/resources/ds_1/access/users/0000-0002-3456-7890",
        json={"permission": "owner"},
    )

    assert resp.status_code == 403


def test_revoke_resource_access(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])
    client.put("/resources/ds_1/access/users/0000-0002-3456-7890", json={"permission": "editor"})

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


def test_revoke_authorizes_before_resolving_or_inspecting_target(client, db_engine, auth_as):
    _setup(db_engine)
    with Session(db_engine) as session:
        grantee_group = session.exec(
            select(AccessGroup).where(AccessGroup.group_name == "0000-0002-3456-7890")
        ).one()
        no_grant_user = User(
            first_name="C",
            last_name="NoGrant",
            email="c@x.com",
            unique_id="0000-0003-4567-8901",
        )
        session.add(no_grant_user)
        session.add(AccessGroup(
            group_name=no_grant_user.unique_id,
            group_type=AccessGroupType.IDENTITY.value,
        ))
        session.add(ResourceACL(
            mfid="ds_1",
            accessgroup_id=grantee_group.id,
            read=True,
            write=True,
            permission=Role.EDITOR.db_value,
        ))
        session.commit()

    auth_as("unauthorized", groups=[("unrelated", 999, Role.VIEWER)])
    targets = [
        "0000-0001-2345-6789",
        "0000-0002-3456-7890",
        "0000-0003-4567-8901",
        "0000-9999-9999-9999",
    ]
    for target in targets:
        resp = client.delete(f"/resources/ds_1/access/users/{target}")
        assert resp.status_code == 403


def test_publish_and_unpublish_resource(client, db_engine, auth_as):
    ag_id = _setup(db_engine)
    auth_as("0000-0001-2345-6789", groups=[("0000-0001-2345-6789", ag_id, Role.OWNER)])

    resp = client.put("/resources/ds_1/access/public")
    assert resp.status_code == 200, resp.text
    assert resp.json()["permission"] == "viewer"
    assert resp.json()["principal_type"] == "public"

    with Session(db_engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == "ds_1")).one()
        assert resource.public is True
        public_acl = session.exec(
            select(ResourceACL)
            .join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id)
            .where(ResourceACL.mfid == "ds_1", AccessGroup.group_name == PUBLIC_GROUP_NAME)
        ).one()
        assert public_acl.permission == Role.VIEWER.db_value
        assert public_acl.read is True
        assert public_acl.write is False

    resp = client.put("/resources/ds_1/access/public")
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        public_acls = session.exec(
            select(ResourceACL)
            .join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id)
            .where(ResourceACL.mfid == "ds_1", AccessGroup.group_name == PUBLIC_GROUP_NAME)
        ).all()
        assert len(public_acls) == 1

    resp = client.delete("/resources/ds_1/access/public")
    assert resp.status_code == 200, resp.text

    with Session(db_engine) as session:
        resource = session.exec(select(Resource).where(Resource.mfid == "ds_1")).one()
        assert resource.public is False
        public_acl = session.exec(
            select(ResourceACL)
            .join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id)
            .where(ResourceACL.mfid == "ds_1", AccessGroup.group_name == PUBLIC_GROUP_NAME)
        ).one_or_none()
        assert public_acl is None


def test_database_rejects_null_acl_permission(db_engine):
    _setup(db_engine)
    with Session(db_engine) as session:
        public_group = session.exec(
            select(AccessGroup).where(AccessGroup.group_name == PUBLIC_GROUP_NAME)
        ).one()
        session.add(ResourceACL(
            mfid="ds_1",
            accessgroup_id=public_group.id,
            read=False,
            write=True,
            permission=None,
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_database_rejects_multiple_owner_acls(db_engine):
    _setup(db_engine)
    with Session(db_engine) as session:
        grantee_group = session.exec(
            select(AccessGroup).where(AccessGroup.group_name == "0000-0002-3456-7890")
        ).one()
        session.add(ResourceACL(
            mfid="ds_1",
            accessgroup_id=grantee_group.id,
            read=True,
            write=True,
            permission=Role.OWNER.db_value,
        ))
        with pytest.raises(IntegrityError):
            session.commit()


@pytest.mark.parametrize(
    "read,write,expected",
    [(True, False, "viewer"), (True, True, "editor")],
)
def test_legacy_access_group_route_maps_flags(client, db_engine, auth_as, read, write, expected):
    _setup(db_engine)
    auth_as("platform-admin", groups=[], platform_role=PlatformRole.ADMIN)
    resp = client.post(
        "/resources/ds_1/access_groups",
        params={
            "group_name": "0000-0002-3456-7890",
            "read": str(read).lower(),
            "write": str(write).lower(),
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["permission"] == expected


def test_legacy_access_group_route_rejects_no_read(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", groups=[], platform_role=PlatformRole.ADMIN)
    resp = client.post(
        "/resources/ds_1/access_groups",
        params={
            "group_name": "0000-0002-3456-7890",
            "read": "false",
            "write": "false",
        },
    )
    assert resp.status_code == 422


def test_legacy_access_group_route_updates_existing_non_owner_grant(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", groups=[], platform_role=PlatformRole.ADMIN)
    first = client.post(
        "/resources/ds_1/access_groups",
        params={
            "group_name": "0000-0002-3456-7890",
            "read": "true",
            "write": "true",
        },
    )
    assert first.status_code == 200, first.text

    updated = client.post(
        "/resources/ds_1/access_groups",
        params={
            "group_name": "0000-0002-3456-7890",
            "read": "true",
            "write": "false",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["permission"] == Role.VIEWER.db_value
    assert updated.json()["read"] is True
    assert updated.json()["write"] is False


def test_legacy_access_group_route_refuses_to_change_owner(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("platform-admin", groups=[], platform_role=PlatformRole.ADMIN)

    resp = client.post(
        "/resources/ds_1/access_groups",
        params={
            "group_name": "0000-0001-2345-6789",
            "read": "true",
            "write": "false",
        },
    )
    assert resp.status_code == 409
    assert "transfer_ownership" in resp.json()["detail"]


def test_legacy_access_group_routes_are_deprecated_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    route = paths["/resources/{mfid}/access_groups"]
    assert route["get"]["deprecated"] is True
    assert route["post"]["deprecated"] is True
