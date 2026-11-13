"""Instrument record maintainers, their visibility, and the maintainer affiliation filter."""

from sqlmodel import Session, select

from app.tables import AccessGroup, ResourceACL, User
from app.models.query_params import InstrumentAffiliation, InstrumentSearchQuery
from app.utils.auth_utils import load_principal
from app.utils.permissions import AccessGroupType
from tests.test_instrument_lifecycle import _user
from tests.people import internal_id

OWNER = "0000-0001-0000-0001"
EDITOR = "0000-0001-0000-0002"
ADMIN = "0000-0001-0000-0003"
STRANGER = "0000-0001-0000-0004"
SA = "0sa0000000000000000000000a"


def _as(auth_as, db_engine, user_id):
    with Session(db_engine) as session:
        principal = load_principal(session, internal_id(session, user_id))
    auth_as(
        user_id,
        groups=[(g.group_name, g.id, g.role, g.group_type) for g in principal.groups],
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
    _as(auth_as, db_engine, OWNER)
    assert client.post(f"/instruments/{mfid}/users/{EDITOR}", params={"role": "editor"}).status_code == 200
    assert client.post(f"/instruments/{mfid}/users/{ADMIN}", params={"role": "admin"}).status_code == 200
    assert client.post(f"/instruments/{mfid}/service_accounts/{SA}").status_code == 200
    return mfid


def _member_roles(body):
    return {(m["unique_id"], m["role"]) for m in body["members"]}


EXPECTED_MEMBERS = {
    (OWNER, "owner"),
    (ADMIN, "admin"),
    (EDITOR, "editor"),
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
        assert [m["role"] for m in members] == ["owner", "admin", "editor"]

    def test_operator_group_and_bound_service_accounts_are_not_members(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        members = client.get(f"/instruments/{mfid}", params={"include_members": True}).json()["members"]
        assert not any(m["is_service_account"] for m in members)
        assert SA not in {m["unique_id"] for m in members}

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


class TestOperatorList:
    def test_role_holders_see_operators_and_only_admins_may_remove(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        for user_id, can_remove in ((OWNER, True), (ADMIN, True), (EDITOR, False)):
            _as(auth_as, db_engine, user_id)
            resp = client.get(f"/instruments/{mfid}/service_accounts")
            assert resp.status_code == 200, resp.text
            operators = resp.json()
            assert [o["unique_id"] for o in operators] == [SA]
            assert operators[0]["capabilities"] == {"can_remove": can_remove, "can_change_role": False, "assignable_roles": []}

    def test_stranger_and_public_only_callers_cannot_see_operators(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, STRANGER)
        assert client.get(f"/instruments/{mfid}/service_accounts").status_code == 403
        with Session(db_engine) as session:
            public = AccessGroup(group_name="public", group_type=AccessGroupType.SYSTEM.value)
            session.add(public)
            session.commit()
            session.refresh(public)
            session.add(ResourceACL(mfid=mfid, accessgroup_id=public.id, read=True, write=False, permission="viewer"))
            session.commit()
            public_id = public.id
        auth_as(STRANGER, groups=[("public", public_id)])
        assert client.get(f"/instruments/{mfid}/service_accounts").status_code == 403

    def test_editor_still_cannot_bind_or_unbind(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, EDITOR)
        assert client.post(f"/instruments/{mfid}/service_accounts/{SA}").status_code == 403
        assert client.delete(f"/instruments/{mfid}/service_accounts/{SA}").status_code == 403

    def test_bind_response_carries_admin_capabilities(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        _as(auth_as, db_engine, OWNER)
        operators = client.post(f"/instruments/{mfid}/service_accounts/{SA}").json()
        assert operators[0]["capabilities"]["can_remove"] is True


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
        _as(auth_as, db_engine, EDITOR)
        items = client.get("/instruments", params=[("affiliation", "owner"), ("affiliation", "maintainer")]).json()["items"]
        assert {i["unique_id"] for i in items} == {mfid, own}

    def test_search_accepts_maintainer(self):
        params = InstrumentSearchQuery(q="xrd", affiliation=["maintainer"])
        assert params.affiliation == [InstrumentAffiliation.MAINTAINER]

    def test_member_affiliation_matches_any_role(self, client, db_engine, auth_as):
        mfid = _setup(client, db_engine, auth_as)
        assert client.post(f"/instruments/{mfid}/users/{STRANGER}", params={"role": "viewer"}).status_code == 200
        for user_id in (OWNER, ADMIN, EDITOR, STRANGER):
            _as(auth_as, db_engine, user_id)
            items = client.get("/instruments", params={"affiliation": "member"}).json()["items"]
            assert [i["unique_id"] for i in items] == [mfid]
