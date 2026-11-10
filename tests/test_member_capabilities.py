import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.api.main import app
from app.tables import AccessGroup, Dataset, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import PlatformRole, Role
from tests.test_join_request_workflow import ALICE, CONTRIBUTOR, EDITOR, OWNER, act, world  # noqa: F401

ROLES = ("viewer", "contributor", "editor", "admin")
CALLERS = {
    "owner": (OWNER, {}),
    "admin": (ALICE, {}),
    "editor": (EDITOR, {}),
    "contributor": (CONTRIBUTOR, {}),
    "platform admin": ("platform-admin", {"platform_role": PlatformRole.ADMIN}),
    "service account": (EDITOR, {"is_service_account": True}),
}


@pytest.fixture
def project(act, world):  # noqa: F811
    assert act(OWNER).post(f"/projects/films/users/{ALICE}", params={"role": "admin"}).status_code == 200
    return act


@pytest.mark.parametrize("name", list(CALLERS))
@pytest.mark.parametrize("target", [OWNER, ALICE, EDITOR, CONTRIBUTOR])
def test_capabilities_match_what_the_routes_allow(project, world, auth_as, name, target):  # noqa: F811
    orcid, kwargs = CALLERS[name]
    group_id, mfid = world["films"]
    standing = {OWNER: Role.OWNER, ALICE: Role.ADMIN, EDITOR: Role.EDITOR, CONTRIBUTOR: Role.CONTRIBUTOR}

    def as_caller():
        groups = [(mfid, group_id, standing[orcid])] if orcid in standing else []
        auth_as(orcid, groups=groups, **kwargs)
        return TestClient(app)

    def as_platform_admin():
        auth_as("restorer", groups=[], platform_role=PlatformRole.ADMIN)
        return TestClient(app)

    client = as_caller()
    listing = client.get("/projects/films/users")
    assert listing.status_code == 200, listing.text
    entry = next(member for member in listing.json()["items"] if member["unique_id"] == target)
    capabilities, current = entry["capabilities"], entry["role"]

    accepted = []
    for role in ROLES:
        if role == current:
            continue
        client = as_caller()
        if client.patch(f"/projects/films/users/{target}", params={"role": role}).status_code == 200:
            accepted.append(role)
            assert as_platform_admin().patch(f"/projects/films/users/{target}", params={"role": current}).status_code == 200
    assert sorted(accepted) == sorted(set(capabilities["assignable_roles"]) - {current})
    assert capabilities["can_change_role"] == bool(accepted)

    removed = as_caller().delete(f"/projects/films/users/{target}").status_code == 200
    assert capabilities["can_remove"] == removed


@pytest.fixture
def shared_dataset(world, db_engine):  # noqa: F811
    films_group, films_mfid = world["films"]
    bulk_group, _ = world["bulk"]
    with Session(db_engine) as session:
        alice = session.exec(select(User).where(User.unique_id == ALICE)).one()
        identity = AccessGroup(group_name=ALICE, group_type="identity")
        session.add(identity)
        session.flush()
        session.add(UserAccessGroupLink(user_id=alice.id, accessgroup_id=identity.id, role="owner"))
        session.add(Resource(mfid="ds-shared", resource_type="dataset"))
        session.add(Dataset(unique_id="ds-shared", dataset_name="d", project_mfid=films_mfid))
        for group, permission in ((films_group, "admin"), (bulk_group, "viewer"), (identity.id, "editor")):
            session.add(ResourceACL(mfid="ds-shared", accessgroup_id=group, read=True, write=True, permission=permission))
        session.commit()
    return "ds-shared"


@pytest.mark.parametrize("name", ["owner", "editor", "platform admin", "service account"])
def test_grant_capabilities_match_what_the_routes_allow(world, auth_as, shared_dataset, name):  # noqa: F811
    orcid, kwargs = CALLERS[name]
    group_id, mfid = world["films"]
    standing = {OWNER: Role.OWNER, EDITOR: Role.EDITOR}

    def client_for(who, **extra):
        auth_as(who, groups=[(mfid, group_id, standing[who])] if who in standing else [], **extra)
        return TestClient(app)

    restorer = lambda: client_for("restorer", platform_role=PlatformRole.ADMIN)  # noqa: E731
    caller_listing = client_for(orcid, **kwargs).get(f"/resources/{shared_dataset}/access")
    if caller_listing.status_code != 200:
        assert name in ("editor", "service account")
        return
    grants = caller_listing.json()
    assert {grant["principal_type"] for grant in grants} >= {"user", "project"}
    for grant in grants:
        if grant["principal_type"] == "user":
            path = f"users/{grant['principal_id']}"
        else:
            path = f"projects/{grant['slug']}"
        current = grant["permission"]
        accepted = []
        for role in ROLES:
            if role == current:
                continue
            if client_for(orcid, **kwargs).put(f"/resources/{shared_dataset}/access/{path}", json={"permission": role}).status_code == 200:
                accepted.append(role)
                restorer().put(f"/resources/{shared_dataset}/access/{path}", json={"permission": current})
        assert sorted(accepted) == sorted(set(grant["capabilities"]["assignable_roles"]) - {current}), grant
        removed = client_for(orcid, **kwargs).delete(f"/resources/{shared_dataset}/access/{path}").status_code == 200
        assert grant["capabilities"]["can_remove"] == removed, grant
        if removed:
            restorer().put(f"/resources/{shared_dataset}/access/{path}", json={"permission": current})
