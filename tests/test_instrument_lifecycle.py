"""POST /instruments creates the instrument's operator group (named by its
MFID) and an editor ACL entry on the instrument resource, instead of the
group coming into existence lazily inside create_dataset. create_dataset and
update_dataset reject an unregistered instrument_name instead of minting a
group for it. POST/DELETE .../service_accounts/{sa_id} bind an SA to that
group at admin standing.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Dataset, Instrument, ResourceACL, User
from app.utils.permissions import PlatformRole, Role


def _user(session, orcid, is_service_account=False):
    u = User(first_name="T", last_name=orcid[-4:], email=f"{orcid}@x.com",
            unique_id=orcid, is_service_account=is_service_account)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


INSTRUMENT_PAYLOAD = {
    "instrument_id": "xrd-1",
    "instrument_name": "XRD-1",
    "owner": "facility",
    "location": "b30",
}


class TestCreateInstrument:
    def test_creates_operator_group_with_editor_acl(self, client, db_engine, auth_as):
        auth_as("admin-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        assert resp.status_code == 200, resp.text
        mfid = resp.json()["unique_id"]

        with Session(db_engine) as session:
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == mfid)).one()
            acl = session.exec(select(ResourceACL).where(
                ResourceACL.accessgroup_id == ag.id, ResourceACL.mfid == mfid)).one()
            assert acl.role == Role.EDITOR.db_value

    def test_rejects_duplicate_instrument_id(self, client, db_engine, auth_as):
        auth_as("admin-2", groups=[], platform_role=PlatformRole.ADMIN)
        client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        resp = client.post("/instruments", json={**INSTRUMENT_PAYLOAD, "instrument_name": "a different name"})
        assert resp.status_code == 409

    def test_rejects_malformed_instrument_id(self, client, db_engine, auth_as):
        auth_as("admin-3", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json={**INSTRUMENT_PAYLOAD, "instrument_id": "has a space"})
        assert resp.status_code == 422

    def test_non_admin_is_rejected(self, client, db_engine, auth_as):
        auth_as("0000-0001", groups=[])
        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        assert resp.status_code == 403


class TestServiceAccountBinding:
    def _create_instrument(self, client, auth_as):
        auth_as("admin-4", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        return resp.json()["unique_id"]

    def test_bind_adds_sa_at_admin_standing(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "sa-1", is_service_account=True)

        auth_as("admin-5", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post(f"/instruments/{mfid}/service_accounts/sa-1")
        assert resp.status_code == 200, resp.text
        members = resp.json()
        assert len(members) == 1
        assert members[0]["unique_id"] == "sa-1"
        assert members[0]["role"] == "admin"

    def test_bind_rejects_non_service_account(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0002")

        auth_as("admin-6", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post(f"/instruments/{mfid}/service_accounts/0000-0002")
        assert resp.status_code == 404

    def test_bind_rejects_unknown_instrument(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "sa-2", is_service_account=True)

        auth_as("admin-7", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments/no-such-mfid/service_accounts/sa-2")
        assert resp.status_code == 404

    def test_non_admin_cannot_bind(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "sa-3", is_service_account=True)

        auth_as("0000-0003", groups=[])
        resp = client.post(f"/instruments/{mfid}/service_accounts/sa-3")
        assert resp.status_code == 403

    def test_unbind_removes_the_sa(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "sa-4", is_service_account=True)

        auth_as("admin-8", groups=[], platform_role=PlatformRole.ADMIN)
        client.post(f"/instruments/{mfid}/service_accounts/sa-4")
        resp = client.delete(f"/instruments/{mfid}/service_accounts/sa-4")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_unbind_a_never_bound_sa_is_404(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "sa-5", is_service_account=True)

        auth_as("admin-9", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.delete(f"/instruments/{mfid}/service_accounts/sa-5")
        assert resp.status_code == 404


class TestDatasetInstrumentResolution:
    def test_registered_instrument_grants_contributor_to_its_operator_group(self, client, db_engine, auth_as):
        auth_as("admin-10", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        mfid = resp.json()["unique_id"]

        auth_as("0000-0004", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d1", "instrument_name": "XRD-1"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d1")).one()
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == mfid)).one()
            acl = session.exec(select(ResourceACL).where(
                ResourceACL.accessgroup_id == ag.id, ResourceACL.mfid == ds.unique_id)).one()
            assert acl.role == Role.CONTRIBUTOR.db_value

    def test_unregistered_instrument_name_is_rejected(self, client, db_engine, auth_as):
        auth_as("0000-0005", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d2", "instrument_name": "no-such-instrument"})
        assert resp.status_code == 400

    def test_instrument_name_match_is_case_insensitive(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            session.add(Instrument(unique_id="inst-ci-1", instrument_id="ci-1",
                                   instrument_name="hip_microscope", owner="lab", location="b30"))
            session.add(AccessGroup(group_name="inst-ci-1"))
            session.commit()

        auth_as("0000-0006", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d3", "instrument_name": "HiP_Microscope"})
        assert resp.status_code == 200, resp.text

    def test_update_dataset_rejects_unregistered_instrument(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0007")
            session.add(AccessGroup(group_name="0000-0007"))
            session.commit()

        auth_as("0000-0007", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d4"})
        dsid = resp.json()["unique_id"]

        resp = client.patch(f"/datasets/{dsid}", json={"instrument_name": "no-such-instrument"})
        assert resp.status_code == 400
