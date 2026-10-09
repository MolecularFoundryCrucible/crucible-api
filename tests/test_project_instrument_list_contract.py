"""GET /projects and GET /instruments: caller role, sorting, and the member filter."""
from datetime import datetime

from sqlmodel import Session

from app.models.core_models import (
    AccessGroup, Instrument, Project, Resource, ResourceACL, User, UserAccessGroupLink,
)
from app.utils.permissions import PUBLIC_GROUP_NAME, AccessGroupType, PlatformRole, Role

ALICE = "0000-0001-0000-0001"
BOB = "0000-0002-0000-0002"

PROJECTS = [
    # slug, title, created, modified
    ("proj-b", "Zeta study", "2026-01-02T00:00:00+00:00", "2026-03-01T00:00:00+00:00"),
    ("proj-a", "alpha study", "2026-01-03T00:00:00+00:00", "2026-01-03T00:00:00+00:00"),
    ("proj-c", None, "2026-01-01T00:00:00+00:00", "2026-01-05T00:00:00+00:00"),
]

INSTRUMENTS = [
    # slug, name, created
    ("xrd-2", "beta diffractometer", "2026-01-01T00:00:00+00:00"),
    ("XRD-1", "Alpha microscope", "2026-01-03T00:00:00+00:00"),
    ("sem-9", "gamma SEM", "2026-01-02T00:00:00+00:00"),
]


def _ts(value):
    return datetime.fromisoformat(value) if value else None


def _seed(db_engine):
    """Alice owns proj-b and is a viewer of proj-a; Bob is an editor of proj-a.
    Alice owns XRD-1, is an editor of xrd-2, and sem-9 is only public."""
    ids = {}
    with Session(db_engine) as session:
        alice = User(unique_id=ALICE, first_name="Alice", last_name="A", username="alice")
        bob = User(unique_id=BOB, first_name="Bob", last_name="B", username="bob")
        session.add_all([alice, bob])
        alice_ag = AccessGroup(group_name=ALICE, group_type=AccessGroupType.IDENTITY.value)
        public_ag = AccessGroup(group_name=PUBLIC_GROUP_NAME, group_type=AccessGroupType.SYSTEM.value)
        session.add_all([alice_ag, public_ag])
        session.commit()
        for row in (alice, bob, alice_ag, public_ag):
            session.refresh(row)
        ids.update(alice=alice.id, bob=bob.id, alice_ag=alice_ag.id, public_ag=public_ag.id)

        for slug, title, created, modified in PROJECTS:
            project = Project(project_id=slug, title=title, organization="LBNL", project_lead_orcid=ALICE)
            session.add(project)
            session.commit()
            session.refresh(project)
            group = AccessGroup(group_name=project.unique_id, group_type=AccessGroupType.PROJECT.value)
            session.add(group)
            session.add(Resource(mfid=project.unique_id, resource_type="project",
                                 creation_time=_ts(created), modification_time=_ts(modified)))
            session.commit()
            session.refresh(group)
            ids[slug] = project.unique_id
            ids[f"{slug}:ag"] = group.id
            session.add(ResourceACL(mfid=project.unique_id, accessgroup_id=group.id, read=True,
                                    write=True, permission=Role.OWNER.db_value))
        session.add_all([
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=ids["proj-b:ag"], role="owner"),
            UserAccessGroupLink(user_id=alice.id, accessgroup_id=ids["proj-a:ag"], role="viewer"),
            UserAccessGroupLink(user_id=bob.id, accessgroup_id=ids["proj-a:ag"], role="editor"),
        ])

        for slug, name, created in INSTRUMENTS:
            instrument = Instrument(instrument_id=slug, instrument_name=name, owner=ALICE,
                                    location="b2", status="active")
            session.add(instrument)
            session.commit()
            session.refresh(instrument)
            session.add(Resource(mfid=instrument.unique_id, resource_type="instrument",
                                 creation_time=_ts(created)))
            ids[slug] = instrument.unique_id
        session.add_all([
            ResourceACL(mfid=ids["XRD-1"], accessgroup_id=alice_ag.id, read=True, write=True,
                        permission=Role.OWNER.db_value),
            ResourceACL(mfid=ids["xrd-2"], accessgroup_id=alice_ag.id, read=True, write=True,
                        permission=Role.EDITOR.db_value),
            ResourceACL(mfid=ids["sem-9"], accessgroup_id=public_ag.id, read=True, write=False,
                        permission=Role.VIEWER.db_value),
        ])
        session.commit()
    return ids


def _as_alice(auth_as, ids, **kwargs):
    auth_as(ALICE, groups=[
        (ALICE, ids["alice_ag"], Role.OWNER),
        (PUBLIC_GROUP_NAME, ids["public_ag"], Role.VIEWER),
        (ids["proj-b"], ids["proj-b:ag"], Role.OWNER),
        (ids["proj-a"], ids["proj-a:ag"], Role.VIEWER),
    ], **kwargs)


def _slugs(response, key):
    assert response.status_code == 200, response.text
    return [item[key] for item in response.json()["items"]]


def test_project_list_returns_caller_role(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids, platform_role=PlatformRole.CONTRIBUTOR)

    body = client.get("/projects").json()
    roles = {item["project_id"]: item["role"] for item in body["items"]}
    assert roles == {"proj-a": "viewer", "proj-b": "owner", "proj-c": None}

    detail = client.get(f"/projects/{ids['proj-b']}").json()
    assert detail["role"] == "owner"


def test_project_default_order_is_unchanged(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids, platform_role=PlatformRole.CONTRIBUTOR)

    assert _slugs(client.get("/projects"), "project_id") == ["proj-a", "proj-b", "proj-c"]


def test_project_sorts(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids, platform_role=PlatformRole.CONTRIBUTOR)

    def order(**params):
        return _slugs(client.get("/projects", params=params), "project_id")

    assert order(sort="project_id", direction="desc") == ["proj-c", "proj-b", "proj-a"]
    assert order(sort="title") == ["proj-a", "proj-b", "proj-c"]
    assert order(sort="title", direction="desc") == ["proj-b", "proj-a", "proj-c"]
    assert order(sort="created") == ["proj-a", "proj-b", "proj-c"]
    assert order(sort="created", direction="asc") == ["proj-c", "proj-b", "proj-a"]
    assert order(sort="updated") == ["proj-b", "proj-c", "proj-a"]
    assert client.get("/projects", params={"direction": "asc"}).status_code == 422
    assert client.get("/projects", params={"sort": "nope"}).status_code == 422


def test_project_sorted_cursor_walks_and_binds_sort(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids, platform_role=PlatformRole.CONTRIBUTOR)

    walked, cursor = [], None
    for _ in range(3):
        params = {"sort": "title", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/projects", params=params).json()
        walked += [item["project_id"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert walked == ["proj-a", "proj-b", "proj-c"]
    assert cursor is None

    first = client.get("/projects", params={"sort": "title", "limit": 1}).json()["next_cursor"]
    resorted = client.get("/projects", params={"sort": "created", "cursor": first})
    assert resorted.status_code == 422


def test_member_filter_selects_self_and_roles(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids)

    assert sorted(_slugs(client.get("/projects", params={"member_ref": "alice"}), "project_id")) == ["proj-a", "proj-b"]
    owned = client.get("/projects", params={"member_ref": ALICE, "member_role": "owner"})
    assert _slugs(owned, "project_id") == ["proj-b"]
    either = client.get("/projects", params={"member_ref": ALICE, "member_role": ["owner", "viewer"]})
    assert sorted(_slugs(either, "project_id")) == ["proj-a", "proj-b"]
    assert client.get("/projects", params={"member_role": "owner"}).status_code == 422


def test_member_filter_for_another_user_requires_global_read(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids)
    assert client.get("/projects", params={"member_ref": "bob"}).status_code == 403

    _as_alice(auth_as, ids, platform_role=PlatformRole.CONTRIBUTOR)
    response = client.get("/projects", params={"member_ref": "bob"})
    assert _slugs(response, "project_id") == ["proj-a"]
    assert response.json()["items"][0]["role"] == "viewer"

    both = client.get("/projects", params={"member_ref": ["bob", "alice"]})
    assert _slugs(both, "project_id") == ["proj-a"]


def test_instrument_list_returns_caller_record_role(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids)

    roles = {item["instrument_id"]: item["role"] for item in client.get("/instruments").json()["items"]}
    assert roles == {"XRD-1": "owner", "xrd-2": "editor", "sem-9": None}

    assert client.get(f"/instruments/{ids['xrd-2']}").json()["role"] == "editor"
    assert client.get(f"/instruments/{ids['sem-9']}").json()["role"] is None


def test_instrument_sorts(client, db_engine, auth_as):
    ids = _seed(db_engine)
    _as_alice(auth_as, ids)

    def order(**params):
        return _slugs(client.get("/instruments", params=params), "instrument_id")

    assert order() == ["XRD-1", "sem-9", "xrd-2"]
    assert order(sort="created", direction="asc") == ["xrd-2", "sem-9", "XRD-1"]
    assert order(sort="instrument_id") == ["sem-9", "XRD-1", "xrd-2"]
    assert order(sort="name") == ["XRD-1", "xrd-2", "sem-9"]
    assert order(sort="name", direction="desc") == ["sem-9", "xrd-2", "XRD-1"]

    walked, cursor = [], None
    for _ in range(3):
        params = {"sort": "name", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/instruments", params=params).json()
        walked += [item["instrument_id"] for item in body["items"]]
        cursor = body["next_cursor"]
    assert walked == ["XRD-1", "xrd-2", "sem-9"]
    assert cursor is None

