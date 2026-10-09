"""Path segments named *_ref accept every identifier form for their entity;
body and query pairs (x_id + x_mfid) must agree."""
from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup, Dataset, Instrument, Project, Resource, ResourceACL, User, UserAccessGroupLink,
)
from app.utils.permissions import AccessGroupType, PlatformRole, Role

OWNER = "0000-0001-1111-1111"
MEMBER = "0000-0002-2222-2222"
SA_MFID = "0tkn2knjast3h0008nyq9zps2c"


def _setup(db_engine):
    with Session(db_engine) as session:
        owner = User(first_name="O", last_name="Owner", email="owner@x.com",
                     unique_id=OWNER, username="owner_user")
        member = User(first_name="M", last_name="Member", email="member@x.com",
                      unique_id=MEMBER, username="member_user")
        sa = User(first_name="S", last_name="A", unique_id=SA_MFID,
                  username="beamline_sa", is_service_account=True)
        project = Project(project_id="Proj-Slug", organization="LBNL", project_lead_orcid=OWNER)
        other = Project(project_id="other-proj", organization="LBNL", project_lead_orcid=OWNER)
        instrument = Instrument(instrument_id="XRD_1", instrument_name="XRD", owner=OWNER,
                                location="b2", status="active")
        session.add_all([owner, member, sa, project, other, instrument])
        session.commit()
        for row in (owner, member, sa, project, other, instrument):
            session.refresh(row)

        owner_ag = AccessGroup(group_name=OWNER, group_type=AccessGroupType.IDENTITY.value)
        member_ag = AccessGroup(group_name=MEMBER, group_type=AccessGroupType.IDENTITY.value)
        project_ag = AccessGroup(group_name=project.unique_id, group_type=AccessGroupType.PROJECT.value)
        other_ag = AccessGroup(group_name=other.unique_id, group_type=AccessGroupType.PROJECT.value)
        instrument_ag = AccessGroup(group_name=instrument.unique_id,
                                    group_type=AccessGroupType.INSTRUMENT.value)
        session.add_all([owner_ag, member_ag, project_ag, other_ag, instrument_ag])
        session.add_all([
            Resource(mfid=project.unique_id, resource_type="project"),
            Resource(mfid=other.unique_id, resource_type="project"),
            Resource(mfid=instrument.unique_id, resource_type="instrument"),
            Resource(mfid="ds_1", resource_type="dataset"),
            Dataset(unique_id="ds_1", dataset_name="d", owner_orcid=OWNER,
                    project_id=project.project_id, project_mfid=project.unique_id),
        ])
        session.commit()
        for row in (owner_ag, project_ag, other_ag):
            session.refresh(row)

        session.add_all([
            UserAccessGroupLink(user_id=owner.id, accessgroup_id=owner_ag.id, role="owner"),
            UserAccessGroupLink(user_id=owner.id, accessgroup_id=project_ag.id, role="owner"),
            UserAccessGroupLink(user_id=owner.id, accessgroup_id=other_ag.id, role="owner"),
            UserAccessGroupLink(user_id=member.id, accessgroup_id=project_ag.id, role="viewer"),
            ResourceACL(mfid=instrument.unique_id, accessgroup_id=owner_ag.id, read=True,
                        write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid="ds_1", accessgroup_id=owner_ag.id, read=True, write=True,
                        permission=Role.OWNER.db_value),
            ResourceACL(mfid="ds_1", accessgroup_id=project_ag.id, read=True, write=True,
                        permission=Role.ADMIN.db_value),
            ResourceACL(mfid=project.unique_id, accessgroup_id=project_ag.id, read=True,
                        write=True, permission=Role.OWNER.db_value),
            ResourceACL(mfid=other.unique_id, accessgroup_id=other_ag.id, read=True,
                        write=True, permission=Role.OWNER.db_value),
        ])
        session.commit()
        return {
            "owner_ag": owner_ag.id,
            "project_ag": project_ag.id,
            "other_ag": other_ag.id,
            "project": project.unique_id,
            "other": other.unique_id,
            "instrument": instrument.unique_id,
        }


def _as_owner(auth_as, ids):
    auth_as(OWNER, groups=[
        (OWNER, ids["owner_ag"], Role.OWNER),
        (ids["project"], ids["project_ag"], Role.OWNER),
        (ids["other"], ids["other_ag"], Role.OWNER),
    ])


def _project_grants(db_engine, mfid):
    with Session(db_engine) as session:
        return session.exec(
            select(AccessGroup.group_name, ResourceACL.permission)
            .join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id)
            .where(ResourceACL.mfid == mfid,
                   AccessGroup.group_type == AccessGroupType.PROJECT.value)
        ).all()


def test_project_grant_accepts_project_mfid(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.put(f"/resources/{ids['instrument']}/access/projects/{ids['other']}",
                      json={"permission": "editor"})

    assert resp.status_code == 200, resp.text
    assert _project_grants(db_engine, ids["instrument"]) == [(ids["other"], "editor")]


def test_project_grant_still_accepts_slug_case_insensitively(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.put(f"/resources/{ids['instrument']}/access/projects/OTHER-PROJ",
                      json={"permission": "viewer"})
    assert resp.status_code == 200, resp.text

    resp = client.delete(f"/resources/{ids['instrument']}/access/projects/{ids['other']}")
    assert resp.status_code == 200, resp.text
    assert _project_grants(db_engine, ids["instrument"]) == []


def test_instrument_routes_accept_slug(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.patch("/instruments/xrd_1", json={"location": "b3"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["unique_id"] == ids["instrument"]

    resp = client.post("/instruments/XRD_1/service_accounts/beamline_sa")
    assert resp.status_code == 200, resp.text
    assert [m["unique_id"] for m in resp.json()] == [SA_MFID]

    resp = client.get("/instruments/xrd_1/service_accounts")
    assert [m["unique_id"] for m in resp.json()] == [SA_MFID]

    resp = client.delete(f"/instruments/{ids['instrument']}/service_accounts/{SA_MFID}")
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_service_account_ref_rejects_human_username(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.post("/instruments/XRD_1/service_accounts/member_user")
    assert resp.status_code == 404


def test_service_account_routes_accept_username(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(OWNER, platform_role=PlatformRole.ADMIN)

    resp = client.get("/service_accounts/beamline_sa")
    assert resp.status_code == 200, resp.text
    assert resp.json()["unique_id"] == SA_MFID
    assert client.get("/service_accounts/owner_user").status_code == 404


def test_project_membership_accepts_username_and_mfid(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.patch(f"/projects/{ids['project']}/users/member_user", params={"role": "editor"})
    assert resp.status_code == 200, resp.text
    roles = {m["unique_id"]: m["role"] for m in resp.json()}
    assert roles[MEMBER] == "editor"

    resp = client.delete("/projects/proj-slug/users/member_user")
    assert resp.status_code == 200, resp.text
    assert MEMBER not in {m["unique_id"] for m in resp.json()}


def test_project_membership_deprecated_query_overrides_still_work(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.delete(f"/projects/{ids['project']}/users/ignored", params={"email": "member@x.com"})
    assert resp.status_code == 200, resp.text


def test_reassign_project_accepts_project_mfid(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.post("/resources/ds_1/project", params={"confirm": True},
                       json={"project_mfid": ids["other"]})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["new_project_id"] == "other-proj"
    assert body["new_project_mfid"] == ids["other"]
    assert body["previous_project_id"] == "Proj-Slug"
    assert body["previous_project_mfid"] == ids["project"]
    with Session(db_engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == "ds_1")).one()
        assert (ds.project_id, ds.project_mfid) == ("other-proj", ids["other"])


def test_reassign_project_canonicalizes_slug_casing(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.post("/resources/ds_1/project", json={"project_id": "OTHER-PROJ"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["new_project_id"] == "other-proj"


def test_reassign_project_rejects_disagreeing_pair(client, db_engine, auth_as):
    ids = _setup(db_engine)
    _as_owner(auth_as, ids)

    resp = client.post("/resources/ds_1/project",
                       json={"project_id": "Proj-Slug", "project_mfid": ids["other"]})

    assert resp.status_code == 422


def test_user_routes_accept_username(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as(MEMBER, groups=[])

    resp = client.get("/users/member_user/access_groups")
    assert resp.status_code == 200, resp.text

    resp = client.patch("/users/member_user", json={"first_name": "Renamed"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["first_name"] == "Renamed"

    assert client.patch("/users/owner_user", json={"first_name": "X"}).status_code == 403


def test_dataset_list_filters_by_instrument_slug(client, db_engine, auth_as):
    ids = _setup(db_engine)
    with Session(db_engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == "ds_1")).one()
        ds.instrument_mfid = ids["instrument"]
        session.add(ds)
        session.commit()
    _as_owner(auth_as, ids)

    by_slug = client.get("/datasets", params={"instrument_id": "xrd_1"})
    assert by_slug.status_code == 200, by_slug.text
    assert [d["unique_id"] for d in by_slug.json()["items"]] == ["ds_1"]

    both = client.get("/datasets", params={"instrument_id": "XRD_1", "instrument_mfid": ids["instrument"]})
    assert [d["unique_id"] for d in both.json()["items"]] == ["ds_1"]

    assert client.get("/datasets", params={"instrument_id": "missing"}).status_code == 404
    assert client.get("/datasets", params={"instrument_id": "XRD_1",
                                           "instrument_mfid": ids["other"]}).status_code == 422

    facets = client.get("/datasets/facets", params={"field": "measurement", "instrument_id": "xrd_1"})
    assert facets.status_code == 200, facets.text


def test_thumbnail_and_sample_link_responses_carry_mfids(client, db_engine, auth_as):
    import base64
    import io

    from PIL import Image

    from app.models.core_models import Sample

    ids = _setup(db_engine)
    with Session(db_engine) as session:
        session.add(Sample(unique_id="s_1", sample_name="s", owner_orcid=OWNER,
                           project_id="Proj-Slug", project_mfid=ids["project"]))
        session.add(Resource(mfid="s_1", resource_type="sample"))
        session.add(ResourceACL(mfid="s_1", accessgroup_id=ids["owner_ag"], read=True,
                                write=True, permission=Role.OWNER.db_value))
        session.commit()
    _as_owner(auth_as, ids)

    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    created = client.post("/datasets/ds_1/thumbnails", json={
        "thumbnail_name": "t.png",
        "thumbnail_b64str": base64.b64encode(buffer.getvalue()).decode("ascii"),
    })
    assert created.status_code == 200, created.text
    assert created.json()["dataset_mfid"] == "ds_1"
    assert isinstance(created.json()["dataset_id"], int)
    listed = client.get("/datasets/ds_1/thumbnails")
    assert [t["dataset_mfid"] for t in listed.json()] == ["ds_1"]

    link = client.post("/datasets/ds_1/samples/s_1")
    assert link.status_code == 200, link.text
    body = link.json()
    assert (body["dataset_mfid"], body["sample_mfid"]) == ("ds_1", "s_1")
    assert (body["dataset_id"], body["sample_id"]) == ("ds_1", "s_1")
