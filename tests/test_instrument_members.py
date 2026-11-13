"""Instrument membership: the members routes, read access to the instrument's datasets capped at viewer,
dataset affiliation, transfer, deletion, and the routes that refuse instrument groups."""

from sqlmodel import Session, select

from app.tables import AccessGroup, ResourceACL, User, UserAccessGroupLink
from app.utils.auth_utils import load_principal
from app.utils.permissions import AccessGroupType
from tests.people import internal_id
from tests.test_instrument_lifecycle import _user

OWNER = "0000-0002-0000-0001"
ADMIN = "0000-0002-0000-0002"
EDITOR = "0000-0002-0000-0003"
VIEWER = "0000-0002-0000-0004"
STRANGER = "0000-0002-0000-0005"
SCIENTIST = "0000-0002-0000-0006"
SA = "0sa0000000000000000000000b"


def _as(auth_as, db_engine, user_id):
    with Session(db_engine) as session:
        principal = load_principal(session, internal_id(session, user_id))
    auth_as(
        user_id,
        groups=[(g.group_name, g.id, g.role, g.group_type) for g in principal.groups],
        platform_role=principal.platform_role,
        is_service_account=principal.is_service_account,
    )


def _instrument(client, db_engine, auth_as, instrument_id="xrd-m"):
    _as(auth_as, db_engine, OWNER)
    resp = client.post("/instruments", json={"instrument_id": instrument_id, "instrument_name": instrument_id.upper(), "location": "b30"})
    assert resp.status_code == 200, resp.text
    return resp.json()["unique_id"]


def _world(client, db_engine, auth_as):
    with Session(db_engine) as session:
        for user_id in (OWNER, ADMIN, EDITOR, VIEWER, STRANGER, SCIENTIST):
            _user(session, user_id)
        _user(session, SA, is_service_account=True)
    mfid = _instrument(client, db_engine, auth_as)
    _as(auth_as, db_engine, OWNER)
    for user_id, role in ((ADMIN, "admin"), (EDITOR, "editor"), (VIEWER, "viewer")):
        assert client.post(f"/instruments/{mfid}/users/{user_id}", params={"role": role}).status_code == 200
    assert client.post(f"/instruments/{mfid}/service_accounts/{SA}").status_code == 200
    _as(auth_as, db_engine, SCIENTIST)
    dataset = client.post("/datasets", json={"dataset_name": "measurement", "instrument_mfid": mfid})
    assert dataset.status_code == 200, dataset.text
    return mfid, dataset.json()["unique_id"]


def _roles(members):
    return [(m["unique_id"], m["role"]) for m in members]


class TestMembersRoutes:
    def test_members_are_listed_highest_role_first(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, VIEWER)
        resp = client.get(f"/instruments/{mfid}/users")
        assert resp.status_code == 200, resp.text
        assert _roles(resp.json()["items"]) == [(OWNER, "owner"), (ADMIN, "admin"), (EDITOR, "editor"), (VIEWER, "viewer")]

    def test_non_members_cannot_list_members(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, STRANGER)
        assert client.get(f"/instruments/{mfid}/users").status_code == 403

    def test_include_members_keeps_its_shape(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        members = client.get(f"/instruments/{mfid}", params={"include_members": True}).json()["members"]
        assert [(m["principal_id"], m["principal_type"], m["permission"]) for m in members] == [
            (OWNER, "user", "owner"), (ADMIN, "user", "admin"), (EDITOR, "user", "editor"), (VIEWER, "user", "viewer")]
        assert members[0]["capabilities"] == {"can_remove": False, "can_change_role": False, "assignable_roles": []}
        assert members[1]["capabilities"]["assignable_roles"] == ["viewer", "editor", "admin"]

    def test_editor_cannot_manage_members(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, EDITOR)
        assert client.post(f"/instruments/{mfid}/users/{STRANGER}", params={"role": "viewer"}).status_code == 403
        assert client.delete(f"/instruments/{mfid}/users/{VIEWER}").status_code == 403

    def test_admin_manages_up_to_admin(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, ADMIN)
        assert client.post(f"/instruments/{mfid}/users/{STRANGER}", params={"role": "admin"}).status_code == 200
        assert client.patch(f"/instruments/{mfid}/users/{STRANGER}", params={"role": "viewer"}).status_code == 200
        assert client.delete(f"/instruments/{mfid}/users/{STRANGER}").status_code == 200

    def test_owner_is_set_only_by_transfer(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        assert client.post(f"/instruments/{mfid}/users/{STRANGER}", params={"role": "owner"}).status_code == 422
        assert client.patch(f"/instruments/{mfid}/users/{OWNER}", params={"role": "admin"}).status_code == 409
        assert client.delete(f"/instruments/{mfid}/users/{OWNER}").status_code == 409

    def test_service_accounts_are_not_members(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        assert client.post(f"/instruments/{mfid}/users/{SA}", params={"role": "viewer"}).status_code == 422

    def test_adding_at_another_role_is_a_conflict(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        assert client.post(f"/instruments/{mfid}/users/{VIEWER}", params={"role": "viewer"}).status_code == 200
        assert client.post(f"/instruments/{mfid}/users/{VIEWER}", params={"role": "editor"}).status_code == 409

    def test_members_may_leave(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, VIEWER)
        assert client.delete(f"/instruments/{mfid}/users/{VIEWER}").status_code == 200
        _as(auth_as, db_engine, VIEWER)
        assert client.get(f"/instruments/{mfid}/users").status_code == 403


class TestDatasetAccess:
    def test_every_member_reads_the_instruments_datasets(self, client, db_engine, auth_as):
        _, dataset = _world(client, db_engine, auth_as)
        for user_id in (OWNER, ADMIN, EDITOR, VIEWER):
            _as(auth_as, db_engine, user_id)
            resp = client.get(f"/datasets/{dataset}")
            assert resp.status_code == 200, (user_id, resp.text)
            assert resp.json()["capabilities"]["can_edit"] is False
        _as(auth_as, db_engine, STRANGER)
        assert client.get(f"/datasets/{dataset}").status_code == 403

    def test_membership_never_gives_more_than_read(self, client, db_engine, auth_as):
        _, dataset = _world(client, db_engine, auth_as)
        for user_id in (OWNER, ADMIN):
            _as(auth_as, db_engine, user_id)
            assert client.patch(f"/datasets/{dataset}", json={"dataset_name": "renamed"}).status_code == 403
            assert client.put(f"/resources/{dataset}/access/users/{STRANGER}", json={"permission": "viewer"}).status_code == 403
            assert client.put(f"/resources/{dataset}/access/public").status_code == 403
            assert client.delete(f"/resources/{dataset}", params={"force": True, "confirm": True}).status_code == 403
            capabilities = client.get(f"/datasets/{dataset}").json()["capabilities"]
            assert capabilities["can_edit"] is False and capabilities["can_manage_access"] is False

    def test_project_role_still_applies_alongside_membership(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, SCIENTIST)
        project = client.post("/projects", json={"project_id": "beamtime-1", "organization": "MF", "project_lead_orcid": SCIENTIST})
        assert project.status_code == 200, project.text
        project_mfid = project.json()["unique_id"]
        _as(auth_as, db_engine, SCIENTIST)
        assert client.post(f"/projects/{project_mfid}/users/{ADMIN}", params={"role": "admin"}).status_code == 200
        dataset = client.post("/datasets", json={"dataset_name": "in project", "instrument_mfid": mfid, "project_mfid": project_mfid}).json()["unique_id"]
        _as(auth_as, db_engine, ADMIN)
        assert client.patch(f"/datasets/{dataset}", json={"dataset_name": "renamed"}).status_code == 200

    def test_bound_service_account_keeps_contributor(self, client, db_engine, auth_as):
        _, dataset = _world(client, db_engine, auth_as)
        with Session(db_engine) as session:
            operator = session.exec(select(ResourceACL.permission).join(AccessGroup, AccessGroup.id == ResourceACL.accessgroup_id).where(
                ResourceACL.mfid == dataset, AccessGroup.group_type == AccessGroupType.INSTRUMENT.value)).one()
        assert operator == "contributor"

    def test_reassignment_moves_read_access(self, client, db_engine, auth_as):
        _, dataset = _world(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0002-0000-0099")
        _as(auth_as, db_engine, "0000-0002-0000-0099")
        other = client.post("/instruments", json={"instrument_id": "sem-m", "instrument_name": "SEM", "location": "b30"}).json()["unique_id"]
        _as(auth_as, db_engine, SCIENTIST)
        assert client.put(f"/datasets/{dataset}/instrument", json={"instrument_mfid": other}).status_code == 200
        _as(auth_as, db_engine, VIEWER)
        assert client.get(f"/datasets/{dataset}").status_code == 403
        _as(auth_as, db_engine, "0000-0002-0000-0099")
        assert client.get(f"/datasets/{dataset}").status_code == 200

    def test_dataset_grant_list_shows_the_instrument_members(self, client, db_engine, auth_as):
        mfid, dataset = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, SCIENTIST)
        grants = client.get(f"/resources/{dataset}/access").json()
        members = [g for g in grants if g["principal_type"] == "instrument_members"]
        assert len(members) == 1 and members[0]["principal_id"] == mfid and members[0]["permission"] == "viewer"
        assert members[0]["capabilities"]["can_remove"] is False


class TestAffiliation:
    def test_member_excludes_and_instrument_lists_datasets_read_through_membership(self, client, db_engine, auth_as):
        _, dataset = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, VIEWER)
        own = client.post("/datasets", json={"dataset_name": "mine"}).json()["unique_id"]
        _as(auth_as, db_engine, VIEWER)

        def ids(*values):
            resp = client.get("/datasets", params=[("affiliation", v) for v in values])
            assert resp.status_code == 200, resp.text
            return {item["unique_id"] for item in resp.json()["items"]}

        assert ids("member") == {own}
        assert ids("instrument") == {dataset}
        assert ids("member", "instrument") == {own, dataset}

    def test_instrument_affiliation_works_on_facets_and_search(self, client, db_engine, auth_as):
        from app.models.query_params import DatasetAffiliation, DatasetSearchQuery

        _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, VIEWER)
        facets = client.get("/datasets/facets", params={"field": "data_format", "affiliation": "instrument"})
        assert facets.status_code == 200, facets.text
        assert sum(bucket["count"] for bucket in facets.json()["items"]) == 1
        assert DatasetSearchQuery(q="xrd", affiliation=["instrument"]).affiliation == [DatasetAffiliation.INSTRUMENT]

    def test_samples_refuse_instrument_affiliation(self, client, db_engine, auth_as):
        _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, VIEWER)
        assert client.get("/samples", params={"affiliation": "instrument"}).status_code == 422


class TestTransfer:
    def test_previous_owner_stays_as_admin(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        resp = client.post(f"/resources/{mfid}/transfer_ownership", params={"confirm": True}, json={"new_owner": EDITOR})
        assert resp.status_code == 200, resp.text
        assert resp.json()["previous_owner"]["unique_id"] == OWNER
        _as(auth_as, db_engine, EDITOR)
        roles = dict(_roles(client.get(f"/instruments/{mfid}/users").json()["items"]))
        assert roles[EDITOR] == "owner" and roles[OWNER] == "admin"
        assert client.get(f"/instruments/{mfid}").json()["owner"]["unique_id"] == EDITOR

    def test_service_account_cannot_own(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        resp = client.post(f"/resources/{mfid}/transfer_ownership", params={"confirm": True}, json={"new_owner": SA})
        assert resp.status_code == 422


class TestRefusedRoutes:
    def test_grant_changes_on_an_instrument_point_to_membership(self, client, db_engine, auth_as):
        mfid, _ = _world(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        put = client.put(f"/resources/{mfid}/access/users/{STRANGER}", json={"permission": "editor"})
        assert put.status_code == 409 and put.json()["detail"]["code"] == "use_instrument_members"
        assert client.delete(f"/resources/{mfid}/access/users/{EDITOR}").status_code == 409
        assert client.get(f"/resources/{mfid}/access").status_code == 200

    def test_legacy_routes_refuse_instrument_groups(self, client, db_engine, auth_as):
        mfid, dataset = _world(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "padmin-m")
            session.exec(select(User).where(User.unique_id == "padmin-m")).one().platform_role = "admin"
            session.commit()
        _as(auth_as, db_engine, "padmin-m")
        assert client.post(f"/users/{STRANGER}/access_groups/{mfid}:members").status_code == 409
        assert client.delete(f"/users/{VIEWER}/access_groups/{mfid}:members").status_code == 409
        resp = client.post(f"/resources/{dataset}/access_groups", params={"group_name": f"{mfid}:members", "read": True, "write": False})
        assert resp.status_code == 409


class TestDeletion:
    def test_deleting_the_instrument_removes_its_members_group(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            for user_id in (OWNER, VIEWER):
                _user(session, user_id)
        mfid = _instrument(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        assert client.post(f"/instruments/{mfid}/users/{VIEWER}", params={"role": "viewer"}).status_code == 200
        preview = client.delete(f"/resources/{mfid}", params={"force": True})
        assert preview.status_code == 200, preview.text
        assert preview.json()["memberships_removed"] == 2
        assert client.delete(f"/resources/{mfid}", params={"force": True, "confirm": True}).status_code == 200
        with Session(db_engine) as session:
            assert session.exec(select(AccessGroup).where(AccessGroup.group_name == f"{mfid}:members")).one_or_none() is None
            assert session.exec(select(UserAccessGroupLink).join(AccessGroup, AccessGroup.id == UserAccessGroupLink.accessgroup_id).where(
                AccessGroup.group_type == AccessGroupType.INSTRUMENT_MEMBER.value)).all() == []


def test_grants_stay_consistent_through_the_routes(client, db_engine, auth_as):
    from app.services.instrument_members import grant_problems

    mfid, dataset = _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, OWNER)
    other = client.post("/instruments", json={"instrument_id": "sem-c", "instrument_name": "SEM", "location": "b30"}).json()["unique_id"]
    _as(auth_as, db_engine, SCIENTIST)
    assert client.put(f"/datasets/{dataset}/instrument", json={"instrument_mfid": other}).status_code == 200
    with Session(db_engine) as session:
        assert set(grant_problems(session).values()) == {0}


def test_platform_administrator_adds_members_and_an_owner_to_an_ownerless_instrument(client, db_engine, auth_as):
    from app.tables import Instrument, Resource
    from tests.instrument_members_seed import seed_instrument_members

    with Session(db_engine) as session:
        for user_id in (OWNER, VIEWER, "padmin-o"):
            _user(session, user_id)
        session.exec(select(User).where(User.unique_id == "padmin-o")).one().platform_role = "admin"
        session.add_all([Instrument(unique_id="inst-orphan", instrument_id="orphan", instrument_name="Orphan", location="b30"),
                         Resource(mfid="inst-orphan", resource_type="instrument")])
        session.flush()
        seed_instrument_members(session, "inst-orphan", {})
        session.commit()
    _as(auth_as, db_engine, "padmin-o")
    assert client.post("/instruments/orphan/users/" + VIEWER, params={"role": "viewer"}).status_code == 200
    resp = client.post("/resources/inst-orphan/transfer_ownership", params={"confirm": True}, json={"new_owner": OWNER})
    assert resp.status_code == 200, resp.text
    assert resp.json()["previous_owner"] is None
    _as(auth_as, db_engine, OWNER)
    assert _roles(client.get("/instruments/orphan/users").json()["items"]) == [(OWNER, "owner"), (VIEWER, "viewer")]
