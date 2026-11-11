import pytest
from sqlmodel import Session, select

from app.tables import AccessGroup, Dataset, Resource, ResourceACL, Sample, User
from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType, PlatformRole, Role

ME = "0000-0007-0000-0001"


@pytest.fixture
def records(db_engine):
    with Session(db_engine) as session:
        public = AccessGroup(group_name=PUBLIC_GROUP_NAME, group_type=AccessGroupType.SYSTEM.value)
        project = AccessGroup(group_name="my-project")
        mine = AccessGroup(group_name=ME, group_type=AccessGroupType.IDENTITY.value)
        session.add_all([public, project, mine])
        session.flush()
        grants = {
            "public-only": [(public.id, "viewer")],
            "public-and-project": [(public.id, "viewer"), (project.id, "admin")],
            "private-project": [(project.id, "admin")],
            "private-mine": [(mine.id, "owner")],
            "private-other": [],
        }
        for mfid, acl in grants.items():
            session.add_all([Resource(mfid=mfid, resource_type="dataset"), Dataset(unique_id=mfid, dataset_name=mfid, public=mfid.startswith("public"))])
            session.add_all([Resource(mfid=f"s-{mfid}", resource_type="sample"), Sample(unique_id=f"s-{mfid}", sample_name=mfid)])
            for group_id, role in acl:
                session.add(ResourceACL(mfid=mfid, accessgroup_id=group_id, read=True, write=role != "viewer", permission=role))
                session.add(ResourceACL(mfid=f"s-{mfid}", accessgroup_id=group_id, read=True, write=role != "viewer", permission=role))
        session.commit()
        return {"public": public.id, "project": project.id, "mine": mine.id}


def _as_me(auth_as, ids, **kwargs):
    auth_as(ME, groups=[(PUBLIC_GROUP_NAME, ids["public"], Role.VIEWER), ("my-project", ids["project"], Role.EDITOR), (ME, ids["mine"], Role.OWNER)], **kwargs)


def _mfids(client, path, params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return sorted(item["unique_id"] for item in response.json()["items"])


@pytest.mark.parametrize("kind, path, prefix", [("datasets", "/datasets", ""), ("samples", "/samples", "s-")])
def test_member_excludes_records_visible_only_because_public(client, auth_as, records, kind, path, prefix):
    _as_me(auth_as, records)
    every = [f"{prefix}{m}" for m in ("private-mine", "private-project", "public-and-project", "public-only")]
    assert _mfids(client, path, {}) == every
    assert _mfids(client, path, {"affiliation": "member"}) == [f"{prefix}{m}" for m in ("private-mine", "private-project", "public-and-project")]
    assert _mfids(client, path, [("affiliation", "member"), ("visibility", "public")]) == [f"{prefix}public-and-project"]
    assert _mfids(client, path, [("affiliation", "owner"), ("affiliation", "member")]) == _mfids(client, path, {"affiliation": "member"})


def test_member_counts_only_own_grants_for_platform_admins(client, auth_as, records):
    auth_as("0000-0007-0000-0002", groups=[], platform_role=PlatformRole.ADMIN)
    assert len(_mfids(client, "/datasets", {})) == 5
    assert _mfids(client, "/datasets", {"affiliation": "member"}) == []


def test_member_applies_to_facets(client, auth_as, records):
    _as_me(auth_as, records)

    def counted(params):
        response = client.get("/datasets/facets", params={"field": "measurement", **params})
        assert response.status_code == 200, response.text
        return sum(item["count"] for item in response.json()["items"])
    assert (counted({}), counted({"affiliation": "member"})) == (4, 3)


def test_projects_keep_owner_only_affiliation(client, auth_as, records):
    _as_me(auth_as, records)
    assert client.get("/projects", params={"affiliation": "member"}).status_code == 422


BOB = "0000-0007-0000-0003"


@pytest.mark.parametrize("path, model, prefix", [("/datasets", Dataset, ""), ("/samples", Sample, "s-")])
def test_affiliation_narrows_an_exact_owner_filter(client, auth_as, db_engine, records, path, model, prefix):
    with Session(db_engine) as session:
        me, bob = User(unique_id=ME, first_name="A", last_name="Me"), User(unique_id=BOB, first_name="B", last_name="Ob")
        session.add_all([me, bob])
        session.flush()
        for mfid, owner in (("private-mine", me), ("private-project", bob), ("public-only", bob)):
            row = session.exec(select(model).where(model.unique_id == f"{prefix}{mfid}")).one()
            row.owner_user_id = owner.id
            session.add(row)
        session.commit()
    _as_me(auth_as, records)
    assert _mfids(client, path, {"owner_id": BOB}) == [f"{prefix}private-project", f"{prefix}public-only"]
    assert _mfids(client, path, {"affiliation": "member", "owner_id": BOB}) == [f"{prefix}private-project"]
    assert _mfids(client, path, {"affiliation": "member", "owner_id": ME}) == [f"{prefix}private-mine"]
    assert _mfids(client, path, {"affiliation": "owner", "owner_id": BOB}) == []
    facets = client.get(f"{path}/facets", params={"field": "owner", "affiliation": "member", "owner_id": BOB})
    assert facets.status_code == 200, facets.text
    assert [(item["value"], item["count"]) for item in facets.json()["items"]] == [(BOB, 1)]

