import pytest
from sqlmodel import Session, select

from app.tables import Dataset, Resource, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import PlatformRole
from tests.test_join_request_workflow import ALICE, BOB, CONTRIBUTOR, EDITOR, OWNER, act, world  # noqa: F401


@pytest.fixture
def dataset(world, db_engine):
    films_group, films_mfid = world["films"]
    bulk_group, _ = world["bulk"]
    with Session(db_engine) as session:
        session.add(Resource(mfid="ds-films", resource_type="dataset"))
        session.add(Dataset(unique_id="ds-films", dataset_name="d", project_mfid=films_mfid))
        session.add(ResourceACL(mfid="ds-films", accessgroup_id=films_group, read=True, write=True, permission="admin"))
        session.commit()
    return "ds-films"


def acl(db_engine, mfid):
    with Session(db_engine) as session:
        return sorted((row.accessgroup_id, row.permission) for row in session.exec(select(ResourceACL).where(ResourceACL.mfid == mfid)))


@pytest.mark.parametrize("method, path", [
    ("put", "/resources/{project}/access/users/" + ALICE),
    ("delete", "/resources/{project}/access/users/" + ALICE),
    ("put", "/resources/{project}/access/projects/bulk"),
    ("post", "/resources/{project}/access_groups?group_name={bulk}&read=true&write=false"),
])
def test_project_access_goes_through_membership(act, world, db_engine, method, path):
    project = world["films"][1]
    before = acl(db_engine, project)
    url = path.format(project=project, bulk=world["bulk"][1])
    client = act("admin-x", platform_role=PlatformRole.ADMIN) if "access_groups" in path else act(OWNER)
    response = getattr(client, method)(url, **({"json": {"permission": "editor"}} if method == "put" else {}))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "use_project_members"
    assert acl(db_engine, project) == before


@pytest.mark.parametrize("method, body", [("put", {"permission": "viewer"}), ("delete", None)])
def test_a_records_own_project_grant_follows_its_assignment(act, world, db_engine, dataset, method, body):
    before = acl(db_engine, dataset)
    response = getattr(act(OWNER), method)(f"/resources/{dataset}/access/projects/films", **({"json": body} if body else {}))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "owning_project_grant"
    assert acl(db_engine, dataset) == before


def test_records_can_still_be_shared_with_other_projects(act, world, db_engine, dataset):
    client = act(OWNER)
    assert client.put(f"/resources/{dataset}/access/projects/bulk", json={"permission": "viewer"}).status_code == 200
    assert (world["bulk"][0], "viewer") in acl(db_engine, dataset)
    assert client.delete(f"/resources/{dataset}/access/projects/bulk").status_code == 200
    assert world["bulk"][0] not in dict(acl(db_engine, dataset))


def role_of(db_engine, world, orcid):
    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == orcid)).one()
        link = session.exec(select(UserAccessGroupLink).where(UserAccessGroupLink.user_id == user.id, UserAccessGroupLink.accessgroup_id == world["films"][0])).one_or_none()
        return link.role if link else None


def test_members_grant_up_to_their_own_role(act, world, db_engine):
    editor = act(EDITOR)
    assert editor.post(f"/projects/films/users/{ALICE}", params={"role": "editor"}).status_code == 200
    assert role_of(db_engine, world, ALICE) == "editor"
    assert editor.post(f"/projects/films/users/{BOB}", params={"role": "admin"}).status_code == 403
    assert act(OWNER).post(f"/projects/films/users/{BOB}", params={"role": "admin"}).status_code == 200
    assert act(OWNER).post(f"/projects/films/users/{EDITOR}", params={"role": "owner"}).status_code == 409


def test_members_remove_others_up_to_their_own_role(act, world, db_engine):
    act(OWNER).post(f"/projects/films/users/{ALICE}", params={"role": "admin"})
    editor = act(EDITOR)
    assert editor.delete(f"/projects/films/users/{ALICE}").status_code == 403
    assert editor.delete(f"/projects/films/users/{CONTRIBUTOR}").status_code == 200
    assert role_of(db_engine, world, CONTRIBUTOR) is None
    assert act(CONTRIBUTOR).delete(f"/projects/films/users/{EDITOR}").status_code == 403
    assert act(OWNER).delete(f"/projects/films/users/{ALICE}").status_code == 200
    assert act(OWNER).delete(f"/projects/films/users/{OWNER}").status_code == 409
    assert act(EDITOR).delete(f"/projects/films/users/{EDITOR}").status_code == 200
