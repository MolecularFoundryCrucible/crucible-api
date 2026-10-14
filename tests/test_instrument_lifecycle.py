"""Instrument ownership, operator groups, service-account bindings, and dataset provenance grants."""
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
    "location": "b30",
}

INSTRUMENT_UPDATE_ROUTE_REQUIRED = (
    "Dataset instrument assignment is not editable here. "
    "Use PUT /datasets/{dataset_mfid}/instrument, which keeps the instrument columns and the resource ACL in step."
)


class TestCreateInstrument:
    def test_creates_operator_group_without_a_grant_on_the_record(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            owner = _user(session, "admin-1")
            owner_group_id = session.exec(select(AccessGroup.id).where(
                AccessGroup.group_name == owner.unique_id
            )).one()

        auth_as("admin-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        assert resp.status_code == 200, resp.text
        mfid = resp.json()["unique_id"]
        assert resp.json()["owner_orcid"] == "admin-1"
        assert resp.json()["owner"]["unique_id"] == "admin-1"
        assert resp.json()["capabilities"] == {
            "can_edit": True,
            "can_manage_access": True,
            "can_change_status": True,
            "can_transfer": True,
            "can_delete": True,
            "max_grant_role": "admin",
        }
        generic_response = client.get(f"/resources/{mfid}")
        assert generic_response.status_code == 200, generic_response.text
        assert generic_response.json()["capabilities"] == resp.json()["capabilities"]

        with Session(db_engine) as session:
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == mfid)).one()
            assert ag.group_type == AccessGroupType.INSTRUMENT.value
            assert session.exec(select(ResourceACL).where(
                ResourceACL.accessgroup_id == ag.id, ResourceACL.mfid == mfid)).one_or_none() is None
            owner_acl = session.exec(select(ResourceACL).where(
                ResourceACL.accessgroup_id == owner_group_id, ResourceACL.mfid == mfid)).one()
            assert owner_acl.permission == Role.OWNER.db_value

    def test_rejects_duplicate_instrument_id(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "admin-2")
        auth_as("admin-2", groups=[], platform_role=PlatformRole.ADMIN)
        client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        resp = client.post("/instruments", json={**INSTRUMENT_PAYLOAD, "instrument_name": "a different name"})
        assert resp.status_code == 409

    def test_rejects_malformed_instrument_id(self, client, db_engine, auth_as):
        auth_as("admin-3", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json={**INSTRUMENT_PAYLOAD, "instrument_id": "has a space"})
        assert resp.status_code == 422

    def test_authenticated_user_can_create_and_becomes_owner(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            owner = _user(session, "0000-0001")
            owner_group_id = session.exec(select(AccessGroup.id).where(
                AccessGroup.group_name == owner.unique_id
            )).one()
        auth_as("0000-0001", groups=[("0000-0001", owner_group_id, Role.OWNER)])
        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_orcid"] == "0000-0001"
        assert resp.json()["owner"]["unique_id"] == "0000-0001"
        assert resp.json()["capabilities"]["can_transfer"] is True

        auth_as("instrument-outsider", groups=[])
        canonical = client.get(f"/instruments/{resp.json()['unique_id']}")
        generic = client.get(f"/resources/{resp.json()['unique_id']}")
        assert canonical.status_code == 200, canonical.text
        assert generic.status_code == 200, generic.text
        assert generic.json() == canonical.json()

    def test_human_cannot_create_for_another_user(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0002")
            _user(session, "0000-0003")
        auth_as("0000-0002", groups=[])
        resp = client.post("/instruments", json={**INSTRUMENT_PAYLOAD, "owner": "0000-0003@x.com"})
        assert resp.status_code == 403
        assert resp.json()["detail"] == "Only platform administrators may create an instrument on behalf of another user"

    def test_platform_admin_can_create_for_another_user(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "admin-3")
            _user(session, "0000-0004")
        auth_as("admin-3", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json={**INSTRUMENT_PAYLOAD, "owner_orcid": "0000-0004"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["owner_orcid"] == "0000-0004"
        assert resp.json()["owner"]["unique_id"] == "0000-0004"

        auth_as(
            "instrument-service-account",
            groups=[],
            platform_role=PlatformRole.ADMIN,
            is_service_account=True,
        )
        rejected = client.post(
            "/instruments",
            json={**INSTRUMENT_PAYLOAD, "instrument_id": "xrd-2"},
        )
        assert rejected.status_code == 403, rejected.text


class TestServiceAccountBinding:
    def _create_instrument(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "admin-4")
        auth_as("admin-4", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        return resp.json()["unique_id"]

    def test_bind_adds_sa_at_admin_standing(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0tp5sa00000000000000000001", is_service_account=True)

        auth_as("admin-5", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post(f"/instruments/{mfid}/service_accounts/0tp5sa00000000000000000001")
        assert resp.status_code == 200, resp.text
        members = resp.json()
        assert len(members) == 1
        assert members[0]["unique_id"] == "0tp5sa00000000000000000001"
        assert members[0]["role"] == "admin"

        with Session(db_engine) as session:
            group_id = session.exec(select(AccessGroup.id).where(
                AccessGroup.group_name == mfid
            )).one()
        auth_as(
            "0tp5sa00000000000000000001",
            groups=[(mfid, group_id, Role.ADMIN)],
            is_service_account=True,
        )
        instrument = client.get(f"/instruments/{mfid}")
        assert instrument.status_code == 200, instrument.text
        assert instrument.json()["capabilities"] == {
            "can_edit": False,
            "can_manage_access": False,
            "can_change_status": False,
            "can_transfer": False,
            "can_delete": False,
            "max_grant_role": None,
        }

    def test_bind_rejects_non_service_account(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0002")

        auth_as("admin-6", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post(f"/instruments/{mfid}/service_accounts/0000-0002")
        assert resp.status_code == 404

    def test_bind_rejects_unknown_instrument(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0tp5sa00000000000000000002", is_service_account=True)

        auth_as("admin-7", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments/no-such-mfid/service_accounts/0tp5sa00000000000000000002")
        assert resp.status_code == 404

    def test_non_admin_cannot_bind(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0tp5sa00000000000000000003", is_service_account=True)

        auth_as("0000-0003", groups=[])
        resp = client.post(f"/instruments/{mfid}/service_accounts/0tp5sa00000000000000000003")
        assert resp.status_code == 403

    def test_unbind_removes_the_sa(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0tp5sa00000000000000000004", is_service_account=True)

        auth_as("admin-8", groups=[], platform_role=PlatformRole.ADMIN)
        client.post(f"/instruments/{mfid}/service_accounts/0tp5sa00000000000000000004")
        resp = client.delete(f"/instruments/{mfid}/service_accounts/0tp5sa00000000000000000004")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_unbind_a_never_bound_sa_is_404(self, client, db_engine, auth_as):
        mfid = self._create_instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0tp5sa00000000000000000005", is_service_account=True)

        auth_as("admin-9", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.delete(f"/instruments/{mfid}/service_accounts/0tp5sa00000000000000000005")
        assert resp.status_code == 404


class TestDatasetInstrumentResolution:
    def test_registered_instrument_grants_contributor_to_its_operator_group(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "admin-10")
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
        with Session(db_engine) as session:
            _user(session, "0000-0005")

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

    def test_update_dataset_rejects_instrument_name_reassignment(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            owner = _user(session, "0000-0007")
            owner_group_id = session.exec(select(AccessGroup.id).where(
                AccessGroup.group_name == owner.unique_id
            )).one()

        auth_as("0000-0007", groups=[("0000-0007", owner_group_id, Role.OWNER)])
        resp = client.post("/datasets", json={"dataset_name": "d4"})
        dsid = resp.json()["unique_id"]

        resp = client.patch(f"/datasets/{dsid}", json={"instrument_name": "no-such-instrument"})
        assert resp.status_code == 409
        assert resp.json()["detail"] == INSTRUMENT_UPDATE_ROUTE_REQUIRED


class TestInstrumentIdOnDataset:
    """POST/PATCH /datasets accept the validated instrument_id slug as an
    alternative to free-text instrument_name; responses resolve back to it."""

    def _register(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "admin-id-1")
        auth_as("admin-id-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json={
            "instrument_id": "xrd-id-1", "instrument_name": "XRD ID 1", "location": "b30",
        })
        return resp.json()

    def test_create_dataset_with_instrument_id_resolves_and_echoes_it(self, client, db_engine, auth_as):
        instrument = self._register(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0010")

        auth_as("0000-0010", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d5", "instrument_id": "xrd-id-1"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["instrument_id"] == "xrd-id-1"
        assert body["instrument_name"] == "XRD ID 1"
        assert body["instrument"] == {
            "unique_id": instrument["unique_id"],
            "instrument_id": "xrd-id-1",
            "instrument_name": "XRD ID 1",
        }
        assert body["project"] is None

        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d5")).one()
            inst = session.exec(select(Instrument).where(Instrument.instrument_id == "xrd-id-1")).one()
            assert ds.instrument_mfid == inst.unique_id

    def test_create_dataset_with_instrument_mfid_resolves_and_echoes_it(self, client, db_engine, auth_as):
        instrument = self._register(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0010-0000-0001")

        auth_as("0000-0010-0000-0001", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d5-mf",
            "instrument_mfid": instrument["unique_id"],
        })

        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument"] == {
            "unique_id": instrument["unique_id"],
            "instrument_id": "xrd-id-1",
            "instrument_name": "XRD ID 1",
        }

    def test_create_dataset_accepts_matching_instrument_id_and_mfid(self, client, db_engine, auth_as):
        instrument = self._register(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0010-0000-0002")

        auth_as("0000-0010-0000-0002", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d5-matching",
            "instrument_id": "XRD-ID-1",
            "instrument_mfid": instrument["unique_id"],
        })

        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument"]["unique_id"] == instrument["unique_id"]

    def test_create_dataset_rejects_conflicting_instrument_id_and_mfid(self, client, db_engine, auth_as):
        first = self._register(client, db_engine, auth_as)
        second_response = client.post("/instruments", json={
            "instrument_id": "xrd-id-2",
            "instrument_name": "XRD ID 2",
            "location": "b30",
        })
        assert second_response.status_code == 200, second_response.text
        second = second_response.json()
        with Session(db_engine) as session:
            _user(session, "0000-0010-0000-0003")

        auth_as("0000-0010-0000-0003", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d5-conflict",
            "instrument_id": first["instrument_id"],
            "instrument_mfid": second["unique_id"],
        })

        assert resp.status_code == 422
        assert resp.json()["detail"] == (
            "instrument_id and instrument_mfid identify different instruments"
        )

    def test_create_dataset_with_unregistered_instrument_id_is_404(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0011")

        auth_as("0000-0011", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d6", "instrument_id": "no-such-slug"})
        assert resp.status_code == 404

    def test_create_dataset_with_unregistered_instrument_mfid_is_404(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0011-0000-0001")

        auth_as("0000-0011-0000-0001", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d6-mf",
            "instrument_mfid": "00000000000000000000000000",
        })

        assert resp.status_code == 404

    def test_create_dataset_via_instrument_name_still_echoes_instrument_id(self, client, db_engine, auth_as):
        """A dataset created the legacy way (free-text instrument_name) still
        gets instrument_id resolved in the response, for clients that adopt it."""
        self._register(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "0000-0012")

        auth_as("0000-0012", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d7", "instrument_name": "XRD ID 1"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument_id"] == "xrd-id-1"

    def test_patch_dataset_rejects_instrument_id_reassignment(self, client, db_engine, auth_as):
        self._register(client, db_engine, auth_as)
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
        assert resp.status_code == 409
        assert resp.json()["detail"] == INSTRUMENT_UPDATE_ROUTE_REQUIRED

    def test_patch_dataset_rejects_unregistered_instrument_id_reassignment(self, client, db_engine, auth_as):
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
        assert resp.status_code == 409
        assert resp.json()["detail"] == INSTRUMENT_UPDATE_ROUTE_REQUIRED


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

    def test_create_dataset_with_project_mfid_sets_canonical_project(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            project = Project(
                project_id="proj_mfid_only_1",
                organization="LBNL",
                project_lead_orcid="0000-0015b",
            )
            session.add(project)
            session.commit()
            session.refresh(project)
            project_mfid = project.unique_id
            group = AccessGroup(
                group_name=project_mfid,
                group_type=AccessGroupType.PROJECT.value,
            )
            session.add(group)
            user = _user(session, "0000-0015b")
            session.commit()
            session.refresh(group)
            session.add(UserAccessGroupLink(
                user_id=user.id,
                accessgroup_id=group.id,
                role="contributor",
            ))
            session.commit()

        auth_as("0000-0015b", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d10-mf-only",
            "project_mfid": project_mfid,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["project_id"] == "proj_mfid_only_1"
        assert resp.json()["project"]["unique_id"] == project_mfid

        with Session(db_engine) as session:
            dataset = session.exec(select(Dataset).where(
                Dataset.dataset_name == "d10-mf-only"
            )).one()
            assert dataset.project_id == "proj_mfid_only_1"
            assert dataset.project_mfid == project_mfid

    def test_create_dataset_with_nonexistent_project_id_is_404_for_a_regular_user(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0016")

        auth_as("0000-0016", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d11", "project_id": "no-such-project"})
        assert resp.status_code == 404
