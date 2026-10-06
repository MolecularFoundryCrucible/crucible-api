"""Instrument record maintainers, their visibility, and the maintainer affiliation filter."""
import importlib.util
from pathlib import Path

from sqlmodel import Session, select

from app.models.core_models import AccessGroup, ResourceACL, User
from app.models.query_params import InstrumentAffiliation, InstrumentSearchQuery
from app.utils.auth_utils import load_principal
from app.utils.permissions import AccessGroupType
from tests.test_instrument_lifecycle import _user

OWNER = "0000-0001-0000-0001"
EDITOR = "0000-0001-0000-0002"
ADMIN = "0000-0001-0000-0003"
STRANGER = "0000-0001-0000-0004"
SA = "0sa0000000000000000000000a"


def _as(auth_as, db_engine, user_id):
    with Session(db_engine) as session:
        principal = load_principal(session, user_id)
    auth_as(
        user_id,
        groups=[(g.group_name, g.id, g.role) for g in principal.groups],
        platform_role=principal.platform_role,
        is_service_account=principal.is_service_account,
    )


def _setup(client, db_engine, auth_as):
    with Session(db_engine) as session:
        for user_id in (OWNER, EDITOR, ADMIN, STRANGER):
            _user(session, user_id)
        _user(session, SA, is_service_account=True)
    _as(auth_as, db_engine, OWNER)
    resp = client.post("/instruments", json={"instrument_id": "xrd-1", "instrument_name": "XRD", "location": "b30"})
    assert resp.status_code == 200, resp.text
    mfid = resp.json()["unique_id"]
    assert client.put(f"/resources/{mfid}/access/users/{EDITOR}", json={"permission": "editor"}).status_code == 200
    assert client.put(f"/resources/{mfid}/access/users/{ADMIN}", json={"permission": "admin"}).status_code == 200
    assert client.post(f"/instruments/{mfid}/service_accounts/{SA}").status_code == 200
    return mfid


def _member_roles(body):
    return {(m["principal_id"], m["principal_type"], m["permission"]) for m in body["members"]}


EXPECTED_MEMBERS = {
    (OWNER, "user", "owner"),
    (ADMIN, "user", "admin"),
    (EDITOR, "user", "editor"),
}


class TestIncludeMembers:
    def test_every_role_holder_sees_the_maintainers(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        for user_id in (OWNER, ADMIN, EDITOR):
            _as(auth_as, db_engine, user_id)
            resp = client.get(f"/instruments/{mfid}", params={"include_members": True})
            assert resp.status_code == 200, resp.text
            assert _member_roles(resp.json()) == EXPECTED_MEMBERS

    def test_members_are_ordered_by_role(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, EDITOR)
        members = client.get(f"/instruments/{mfid}", params={"include_members": True}).json()["members"]
        assert [m["permission"] for m in members] == ["owner", "admin", "editor"]

    def test_operator_group_and_bound_service_accounts_are_not_members(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        members = client.get(f"/instruments/{mfid}", params={"include_members": True}).json()["members"]
        assert all(m["principal_type"] == "user" for m in members)
        assert SA not in {m["principal_id"] for m in members}

    def test_caller_without_a_role_gets_no_members(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, STRANGER)
        resp = client.get(f"/instruments/{mfid}", params={"include_members": True})
        assert resp.status_code == 200
        assert resp.json()["members"] is None

    def test_bound_service_account_gets_no_members(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, SA)
        assert client.get(f"/instruments/{mfid}", params={"include_members": True}).json()["members"] is None

    def test_public_access_alone_does_not_reveal_members(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        with Session(db_engine) as session:
            public = AccessGroup(group_name="public", group_type=AccessGroupType.SYSTEM.value)
            session.add(public)
            session.commit()
            session.refresh(public)
            session.add(ResourceACL(mfid=mfid, accessgroup_id=public.id, read=True, write=False, permission="viewer"))
            session.commit()
            public_id = public.id
        auth_as(STRANGER, groups=[("public", public_id)])
        assert client.get(f"/instruments/{mfid}", params={"include_members": True}).json()["members"] is None

    def test_platform_admin_sees_members(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "padmin")
            session.exec(select(User).where(User.unique_id == "padmin")).one().platform_role = "admin"
            session.commit()
        _as(auth_as, db_engine, "padmin")
        assert _member_roles(client.get(f"/instruments/{mfid}", params={"include_members": True}).json()) == EXPECTED_MEMBERS

    def test_members_are_omitted_by_default(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        assert client.get(f"/instruments/{mfid}").json()["members"] is None

    def test_generic_resource_detail_does_not_expand_members(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        assert client.get(f"/resources/{mfid}").json()["members"] is None

    def test_exact_slug_lookup_includes_members(self, client, db_engine, auth_as):
        _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, EDITOR)
        resp = client.get("/instruments", params={"instrument_id": "xrd-1", "include_members": True})
        assert resp.status_code == 200, resp.text
        assert _member_roles(resp.json()["items"][0]) == EXPECTED_MEMBERS

    def test_collection_include_members_requires_exact_slug(self, client, db_engine, auth_as):
        _setup(client, db_engine, auth_as)
        assert client.get("/instruments", params={"include_members": True}).status_code == 422


class TestMaintainerAffiliation:
    def _second_instrument(self, client):
        resp = client.post("/instruments", json={"instrument_id": "sem-1", "instrument_name": "SEM", "location": "b30"})
        assert resp.status_code == 200, resp.text
        return resp.json()["unique_id"]

    def test_maintainer_matches_editor_and_admin_grants(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        self._second_instrument(client)
        for user_id in (EDITOR, ADMIN):
            _as(auth_as, db_engine, user_id)
            items = client.get("/instruments", params={"affiliation": "maintainer"}).json()["items"]
            assert [i["unique_id"] for i in items] == [mfid]

    def test_owner_is_not_a_maintainer(self, client, db_engine, auth_as):
        _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        assert client.get("/instruments", params={"affiliation": "maintainer"}).json()["items"] == []

    def test_owner_and_maintainer_combine_with_or(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, EDITOR)
        own = self._second_instrument(client)
        items = client.get("/instruments", params=[("affiliation", "owner"), ("affiliation", "maintainer")]).json()["items"]
        assert {i["unique_id"] for i in items} == {mfid, own}

    def test_search_accepts_maintainer(self):
        params = InstrumentSearchQuery(q="xrd", affiliation=["maintainer"])
        assert params.affiliation == [InstrumentAffiliation.MAINTAINER]

    def test_maintainer_does_not_grant_access_to_instrument_datasets(self, client, db_engine, auth_as):
        _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        dsid = client.post("/datasets", json={"dataset_name": "raw", "instrument_id": "xrd-1"}).json()["unique_id"]
        for user_id in (EDITOR, ADMIN):
            _as(auth_as, db_engine, user_id)
            assert client.get(f"/datasets/{dsid}").status_code == 403


def _load_cleanup_script():
    path = Path(__file__).parents[1] / "migrations/data/20261005_remove_instrument_group_record_grants.py"
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_removes_only_the_operator_group_grant_on_its_own_record(client, db_engine, auth_as):
    mfid = _setup(client, db_engine, auth_as)
    dsid = client.post("/datasets", json={"dataset_name": "raw", "instrument_id": "xrd-1"}).json()["unique_id"]
    module = _load_cleanup_script()
    with Session(db_engine) as session:
        group = session.exec(select(AccessGroup).where(AccessGroup.group_name == mfid)).one()
        session.add(ResourceACL(mfid=mfid, accessgroup_id=group.id, read=True, write=True, permission="editor"))
        session.commit()
        group_id = group.id

        assert [row.unique_id for row in module.find_self_grants(session)] == [mfid]
        assert module.delete_self_grants(session) == 1
        session.commit()

        assert module.find_self_grants(session) == []
        assert session.exec(select(ResourceACL).where(
            ResourceACL.mfid == dsid, ResourceACL.accessgroup_id == group_id)).one().permission == "contributor"
        assert len(session.exec(select(ResourceACL).where(ResourceACL.mfid == mfid)).all()) == 3
