"""POST /instruments creates the instrument's operator group (named by its
MFID) and an editor ACL entry on the instrument resource, instead of the
group coming into existence lazily inside create_dataset. create_dataset and
update_dataset reject an unregistered instrument_name instead of minting a
group for it. POST/DELETE .../service_accounts/{sa_id} bind an SA to that
group at admin standing.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Dataset, Instrument, Project, ResourceACL, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PlatformRole, Role


def _user(session, orcid, is_service_account=False):
    u = User(first_name="T", last_name=orcid[-4:], email=f"{orcid}@x.com",
            unique_id=orcid, is_service_account=is_service_account)
    personal_group = AccessGroup(group_name=orcid, group_type=AccessGroupType.IDENTITY.value)
    session.add(u)
    session.add(personal_group)
    session.commit()
    session.refresh(u)
    session.refresh(personal_group)
    session.add(UserAccessGroupLink(
        user_id=u.id,
        accessgroup_id=personal_group.id,
        role=Role.OWNER.db_value,
    ))
    session.commit()
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
            assert acl.permission == Role.EDITOR.db_value

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

        with Session(db_engine) as session:
            _user(session, "0000-0004")

        auth_as("0000-0004", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d1", "instrument_name": "XRD-1"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d1")).one()
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == mfid)).one()
            acl = session.exec(select(ResourceACL).where(
                ResourceACL.accessgroup_id == ag.id, ResourceACL.mfid == ds.unique_id)).one()
            assert acl.permission == Role.CONTRIBUTOR.db_value

    def test_unregistered_instrument_name_is_rejected(self, client, db_engine, auth_as):
        auth_as("0000-0005", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d2", "instrument_name": "no-such-instrument"})
        assert resp.status_code == 400

    def test_instrument_name_match_is_case_insensitive(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            session.add(Instrument(unique_id="inst-ci-1", instrument_id="ci-1",
                                   instrument_name="hip_microscope", owner="lab", location="b30"))
            session.add(AccessGroup(
                group_name="inst-ci-1",
                group_type=AccessGroupType.INSTRUMENT.value,
            ))
            session.commit()
            _user(session, "0000-0006")

        auth_as("0000-0006", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d3", "instrument_name": "HiP_Microscope"})
        assert resp.status_code == 200, resp.text

    def test_update_dataset_rejects_unregistered_instrument(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0007")

        auth_as("0000-0007", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d4"})
        dsid = resp.json()["unique_id"]

        resp = client.patch(f"/datasets/{dsid}", json={"instrument_name": "no-such-instrument"})
        assert resp.status_code == 400


class TestInstrumentIdOnDataset:
    """POST/PATCH /datasets accept the validated instrument_id slug as an
    alternative to free-text instrument_name; responses resolve back to it."""

    def _register(self, client, auth_as):
        auth_as("admin-id-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json={
            "instrument_id": "xrd-id-1", "instrument_name": "XRD ID 1", "owner": "facility", "location": "b30",
        })
        return resp.json()

    def test_create_dataset_with_instrument_id_resolves_and_echoes_it(self, client, db_engine, auth_as):
        self._register(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0010")

        auth_as("0000-0010", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d5", "instrument_id": "xrd-id-1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["instrument_id"] == "xrd-id-1"
        assert body["instrument_name"] == "xrd id 1"

        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d5")).one()
            inst = session.exec(select(Instrument).where(Instrument.instrument_id == "xrd-id-1")).one()
            assert ds.instrument_mfid == inst.unique_id

    def test_create_dataset_with_unregistered_instrument_id_is_400(self, client, db_engine, auth_as):
        auth_as("0000-0011", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d6", "instrument_id": "no-such-slug"})
        assert resp.status_code == 400

    def test_create_dataset_via_instrument_name_still_echoes_instrument_id(self, client, db_engine, auth_as):
        """A dataset created the legacy way (free-text instrument_name) still
        gets instrument_id resolved in the response, for clients that adopt it."""
        self._register(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0012")

        auth_as("0000-0012", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d7", "instrument_name": "XRD ID 1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument_id"] == "xrd-id-1"

    def test_patch_dataset_with_instrument_id(self, client, db_engine, auth_as):
        self._register(client, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0013")
            ag = session.exec(select(AccessGroup).where(
                AccessGroup.group_name == "0000-0013"
            )).one()
            ag_id = ag.id

        auth_as("0000-0013", groups=[("0000-0013", ag_id, Role.OWNER)])
        resp = client.post("/datasets", json={"dataset_name": "d8"})
        dsid = resp.json()["unique_id"]

        resp = client.patch(f"/datasets/{dsid}", json={"instrument_id": "xrd-id-1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument_id"] == "xrd-id-1"

    def test_patch_dataset_with_unregistered_instrument_id_is_400(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0014")
            ag = session.exec(select(AccessGroup).where(
                AccessGroup.group_name == "0000-0014"
            )).one()
            ag_id = ag.id

        auth_as("0000-0014", groups=[("0000-0014", ag_id, Role.OWNER)])
        resp = client.post("/datasets", json={"dataset_name": "d9"})
        dsid = resp.json()["unique_id"]

        resp = client.patch(f"/datasets/{dsid}", json={"instrument_id": "no-such-slug"})
        assert resp.status_code == 400


class TestProjectMfidOnDataset:
    def test_create_dataset_with_project_id_sets_project_mfid(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            proj = Project(project_id="proj_mfid_1", organization="LBNL", project_lead_orcid="0000-0015")
            session.add(proj)
            session.commit()
            session.refresh(proj)
            ag = AccessGroup(group_name=proj.unique_id, group_type=AccessGroupType.PROJECT.value)
            session.add(ag)
            user = _user(session, "0000-0015")
            session.commit()
            session.refresh(ag)
            session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()

        auth_as("0000-0015", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d10", "project_id": "proj_mfid_1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["project_id"] == "proj_mfid_1"

        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d10")).one()
            proj = session.exec(select(Project).where(Project.project_id == "proj_mfid_1")).one()
            assert ds.project_mfid == proj.unique_id

    def test_create_dataset_with_nonexistent_project_id_is_403_for_a_regular_user(self, client, db_engine, auth_as):
        """A non-admin caller with no standing in a nonexistent project hits
        the standing check first — 403, not 404 (see test_create_on_behalf.py
        for the platform-admin case, which does reach the 404)."""
        auth_as("0000-0016", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d11", "project_id": "no-such-project"})
        assert resp.status_code == 403
