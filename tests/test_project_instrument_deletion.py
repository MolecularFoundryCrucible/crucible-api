"""Deleting empty projects and instruments, and the pending state that
surrounds the request."""
from sqlalchemy.sql import literal
from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup,
    AccessGroupJoinRequest,
    Dataset,
    DeletionAuditLog,
    DeletionRequest,
    Instrument,
    Project,
    Resource,
    ResourceACL,
    Sample,
    ScientificMetadataBinary,
    User,
    UserAccessGroupLink,
)
from app.utils.permissions import AccessGroupType, PlatformRole, Role

INSTRUMENT_PAYLOAD = {
    "instrument_id": "xrd-del-1",
    "instrument_name": "XRD deletion",
    "location": "b30",
}


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


def _group_id(session, name):
    return session.exec(
        select(AccessGroup.id).where(AccessGroup.group_name == name)
    ).one()


def _project(db_engine, project_id, lead_orcid):
    """A project with its access group, resource row, owner link, and self ACL."""
    with Session(db_engine) as session:
        lead = _user(session, lead_orcid)
        project = Project(
            project_id=project_id,
            organization="LBNL",
            project_lead_orcid=lead.unique_id,
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        ag = AccessGroup(group_name=project.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add(ag)
        session.add(Resource(mfid=project.unique_id, resource_type="project"))
        session.commit()
        session.refresh(ag)
        session.add(UserAccessGroupLink(
            user_id=lead.id, accessgroup_id=ag.id, role=Role.OWNER.db_value))
        session.add(ResourceACL(
            mfid=project.unique_id, accessgroup_id=ag.id,
            read=True, write=True, permission=Role.OWNER.db_value))
        session.commit()
        return project.unique_id


def _instrument(client, db_engine, auth_as):
    with Session(db_engine) as session:
        _user(session, "instr-admin")
    auth_as("instr-admin", groups=[], platform_role=PlatformRole.ADMIN)
    resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
    assert resp.status_code == 200, resp.text
    return resp.json()["unique_id"]


def _add_member(db_engine, project_mfid, orcid, role=Role.CONTRIBUTOR):
    with Session(db_engine) as session:
        user = _user(session, orcid)
        ag_id = _group_id(session, project_mfid)
        session.add(UserAccessGroupLink(
            user_id=user.id, accessgroup_id=ag_id, role=role.db_value))
        session.commit()


def _join_request(db_engine, project_mfid, orcid):
    with Session(db_engine) as session:
        user = _user(session, orcid)
        ag_id = _group_id(session, project_mfid)
        session.add(AccessGroupJoinRequest(
            accessgroup_id=ag_id, requester_id=user.unique_id))
        session.commit()


def _grant_on_other_resource(db_engine, project_mfid, permission=Role.CONTRIBUTOR):
    """A dataset owned by someone else carrying a grant for the project group."""
    with Session(db_engine) as session:
        _user(session, "other-owner")
        session.add(Resource(mfid="other-ds", resource_type="dataset"))
        session.commit()
        session.add(Dataset(unique_id="other-ds", dataset_name="other",
                            owner_orcid="other-owner"))
        session.commit()
        ag_id = _group_id(session, project_mfid)
        session.add(ResourceACL(
            mfid="other-ds", accessgroup_id=ag_id,
            read=True, write=True, permission=permission.db_value))
        session.commit()


def _create_deletion_request(client, resource_id):
    resp = client.post(f"/resources/{resource_id}/delete")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _approve_request(client, request_id):
    resp = client.patch(f"/deletion_requests/{request_id}", params={"status": "approved"})
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestForcedDeletionProject:
    def test_preview_then_confirm_removes_group_memberships_and_acls(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_force_1", "proj-owner-1")
        project_group_id = _group_id_session(db_engine, project_mfid)
        _add_member(db_engine, project_mfid, "proj-member-1")
        _join_request(db_engine, project_mfid, "proj-joiner-1")
        _grant_on_other_resource(db_engine, project_mfid)
        auth_as("proj-owner-1", groups=[], platform_role=PlatformRole.ADMIN)

        preview = client.delete(f"/resources/{project_mfid}", params={"force": "true"})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["deleted"] is False
        assert body["resource_type"] == "project"
        assert body["blocking"] == {}
        assert body["memberships_removed"] == 2
        assert body["join_requests_removed"] == 1
        assert body["removed_grants"] == [
            {"resource_mfid": "other-ds", "resource_type": "dataset", "permission": "contributor"}
        ]

        with Session(db_engine) as session:
            assert session.get(Resource, project_mfid) is not None
            assert session.exec(select(DeletionAuditLog)).all() == []

        confirmed = client.delete(
            f"/resources/{project_mfid}", params={"force": "true", "confirm": "true"})
        assert confirmed.status_code == 200, confirmed.text
        body = confirmed.json()
        assert body["deleted"] is True
        assert body["memberships_removed"] == 2
        assert body["join_requests_removed"] == 1
        assert body["removed_grants"][0]["resource_mfid"] == "other-ds"

        with Session(db_engine) as session:
            project = session.exec(
                select(Project).where(Project.unique_id == project_mfid)).one_or_none()
            assert project is None
            assert session.get(Resource, project_mfid) is None
            assert session.exec(select(AccessGroup).where(
                AccessGroup.id == project_group_id)).all() == []
            assert session.exec(select(UserAccessGroupLink).where(
                UserAccessGroupLink.accessgroup_id == project_group_id)).all() == []
            assert session.exec(select(AccessGroupJoinRequest)).all() == []
            assert session.exec(select(ResourceACL).where(
                ResourceACL.mfid == "other-ds")).all() == []
            audit = session.exec(select(DeletionAuditLog)).one()
            assert audit.resource_type == "project"
            assert audit.project_mfid == project_mfid
            assert audit.project_id == "proj_force_1"

        with Session(db_engine) as session:
            member = session.exec(
                select(User).where(User.unique_id == "proj-member-1")).one()
            assert member is not None

    def test_request_then_approval_deletes_the_project(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_req_1", "proj-owner-2")
        _add_member(db_engine, project_mfid, "proj-member-2")
        auth_as("proj-owner-2", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])

        created = _create_deletion_request(client, project_mfid)
        assert created["status"] == "pending"
        assert created["preview"]["deleted"] is False
        assert created["preview"]["memberships_removed"] == 2

        approved = _approve_request(client, created["id"])
        assert approved["status"] == "approved"
        assert approved["preview"]["deleted"] is True
        assert approved["preview"]["memberships_removed"] == 2

        with Session(db_engine) as session:
            assert session.exec(
                select(Project).where(Project.unique_id == project_mfid)).one_or_none() is None
            assert session.get(Resource, project_mfid) is None
            assert session.exec(select(AccessGroup).where(
                AccessGroup.group_name == project_mfid)).all() == []
            assert session.exec(select(DeletionRequest)).all() == []
            audit = session.exec(select(DeletionAuditLog)).one()
            assert audit.requester_id == "proj-owner-2"
            assert audit.project_id == "proj_req_1"
            assert audit.project_mfid == project_mfid
            assert audit.resource_name == "proj_req_1"

    def test_request_and_approval_compute_the_summary_once_each(self, client, db_engine, auth_as, monkeypatch):
        from app.utils import deletion_utils
        calls = []
        original = deletion_utils._project_deletion_summary
        monkeypatch.setattr(deletion_utils, "_project_deletion_summary", lambda session, mfid: calls.append(mfid) or original(session, mfid))
        project_mfid = _project(db_engine, "proj_req_once", "proj-owner-3")
        auth_as("proj-owner-3", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])

        created = _create_deletion_request(client, project_mfid)
        assert len(calls) == 1
        _approve_request(client, created["id"])
        assert len(calls) == 2


def _group_id_session(db_engine, name):
    with Session(db_engine) as session:
        return _group_id(session, name)


class TestForcedDeletionInstrument:
    def test_preview_then_confirm_unbinds_service_accounts(self, client, db_engine, auth_as):
        mfid = _instrument(client, db_engine, auth_as)
        operator_group_id = _group_id_session(db_engine, mfid)
        with Session(db_engine) as session:
            _user(session, "0tp5sade1000000000000000a1", is_service_account=True)
        auth_as("sa-admin-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post(f"/instruments/{mfid}/service_accounts/0tp5sade1000000000000000a1")
        assert resp.status_code == 200, resp.text

        preview = client.delete(f"/resources/{mfid}", params={"force": "true"})
        assert preview.status_code == 200, preview.text
        body = preview.json()
        assert body["deleted"] is False
        assert body["resource_type"] == "instrument"
        assert body["service_accounts_unbound"] == 1
        assert body["blocking"] == {}

        confirmed = client.delete(
            f"/resources/{mfid}", params={"force": "true", "confirm": "true"})
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["deleted"] is True
        assert confirmed.json()["service_accounts_unbound"] == 1

        with Session(db_engine) as session:
            assert session.get(Resource, mfid) is None
            assert session.exec(
                select(Instrument).where(Instrument.unique_id == mfid)).one_or_none() is None
            assert session.exec(select(AccessGroup).where(
                AccessGroup.id == operator_group_id)).all() == []
            assert session.exec(select(UserAccessGroupLink).where(
                UserAccessGroupLink.accessgroup_id == operator_group_id)).all() == []
            sa_user = session.exec(
                select(User).where(User.unique_id == "0tp5sade1000000000000000a1")).one()
            assert sa_user is not None
            audit = session.exec(select(DeletionAuditLog)).one()
            assert audit.resource_type == "instrument"
            assert audit.project_id is None
            assert audit.project_mfid is None

    def test_request_then_approval_deletes_the_instrument(self, client, db_engine, auth_as):
        mfid = _instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            owner_group = _group_id(session, "instr-admin")
        auth_as("instr-admin", groups=[("instr-admin", owner_group, Role.OWNER)])

        created = _create_deletion_request(client, mfid)
        assert created["status"] == "pending"
        assert created["preview"]["service_accounts_unbound"] == 0
        assert created["resource_name"] == "XRD deletion"

        approved = _approve_request(client, created["id"])
        assert approved["status"] == "approved"
        assert approved["preview"]["deleted"] is True

        with Session(db_engine) as session:
            assert session.exec(
                select(Instrument).where(Instrument.unique_id == mfid)).one_or_none() is None
            assert session.get(Resource, mfid) is None
            audit = session.exec(select(DeletionAuditLog)).one()
            assert audit.resource_type == "instrument"
            assert audit.resource_name == "XRD deletion"


class TestBlockers:
    def _project_with_dataset(self, db_engine, project_id):
        project_mfid = _project(db_engine, project_id, "block-owner-1")
        with Session(db_engine) as session:
            owner = session.exec(
                select(User).where(User.unique_id == "block-owner-1")).one()
            owner_group = session.exec(
                select(AccessGroup).where(
                    AccessGroup.group_name == owner.unique_id)).one()
            dataset = Dataset(
                unique_id=f"{project_id}-ds",
                dataset_name="blocking dataset",
                owner_orcid=owner.unique_id,
                project_mfid=project_mfid,
            )
            session.add(dataset)
            session.commit()
            session.refresh(dataset)
            session.add(Resource(mfid=dataset.unique_id, resource_type="dataset"))
            session.add(ResourceACL(
                mfid=dataset.unique_id, accessgroup_id=owner_group.id,
                read=True, write=True, permission=Role.OWNER.db_value))
            session.commit()
        return project_mfid

    def _project_with_sample(self, db_engine, project_id):
        project_mfid = _project(db_engine, project_id, "block-owner-2")
        with Session(db_engine) as session:
            owner = session.exec(
                select(User).where(User.unique_id == "block-owner-2")).one()
            session.add(Sample(
                unique_id=f"{project_id}-smp",
                sample_name="blocking sample",
                owner_orcid=owner.unique_id,
                project_mfid=project_mfid,
            ))
            session.commit()
        return project_mfid

    def test_assigned_dataset_blocks_request_approval_and_force(self, client, db_engine, auth_as):
        project_mfid = self._project_with_dataset(db_engine, "proj_block_1")
        auth_as("block-owner-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])

        resp = client.post(f"/resources/{project_mfid}/delete")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["blocking"] == {"datasets": 1}

        with Session(db_engine) as session:
            session.add(DeletionRequest(
                resource_id=project_mfid,
                requester_id="block-owner-1",
                status="pending",
            ))
            session.commit()
            request_id = session.exec(
                select(DeletionRequest).where(
                    DeletionRequest.resource_id == project_mfid)).one().id

        resp = client.patch(f"/deletion_requests/{request_id}", params={"status": "approved"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["blocking"] == {"datasets": 1}

        resp = client.delete(f"/resources/{project_mfid}", params={"force": "true"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["blocking"] == {"datasets": 1}

        with Session(db_engine) as session:
            assert session.get(Resource, project_mfid) is not None
            assert session.exec(select(DeletionAuditLog)).all() == []

    def test_assigned_sample_blocks(self, client, db_engine, auth_as):
        project_mfid = self._project_with_sample(db_engine, "proj_block_2")
        auth_as("block-owner-2", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])

        resp = client.post(f"/resources/{project_mfid}/delete")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["blocking"] == {"samples": 1}

        resp = client.delete(
            f"/resources/{project_mfid}",
            params={"force": "true", "confirm": "true"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["blocking"] == {"samples": 1}

    def test_dataset_assigned_after_request_creation_blocks_approval(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_block_4", "block-owner-4")
        auth_as("block-owner-4", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])

        created = _create_deletion_request(client, project_mfid)

        with Session(db_engine) as session:
            owner = session.exec(
                select(User).where(User.unique_id == "block-owner-4")).one()
            owner_group = session.exec(
                select(AccessGroup).where(
                    AccessGroup.group_name == owner.unique_id)).one()
            session.add(Dataset(
                unique_id="late-ds",
                dataset_name="late dataset",
                owner_orcid=owner.unique_id,
                project_mfid=project_mfid,
            ))
            session.commit()
            session.add(Resource(mfid="late-ds", resource_type="dataset"))
            session.add(ResourceACL(
                mfid="late-ds", accessgroup_id=owner_group.id,
                read=True, write=True, permission=Role.OWNER.db_value))
            session.commit()

        resp = client.patch(f"/deletion_requests/{created['id']}", params={"status": "approved"})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["blocking"] == {"datasets": 1}

        with Session(db_engine) as session:
            assert session.get(Resource, project_mfid) is not None
            request = session.get(DeletionRequest, created["id"])
            assert request.status == "pending"
            assert session.exec(select(DeletionAuditLog)).all() == []


class TestPendingState:
    def _pending_project(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_pending_1", "pending-owner-1")
        auth_as("pending-owner-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])
        created = _create_deletion_request(client, project_mfid)
        return project_mfid, created["id"]

    def _pending_instrument(self, client, db_engine, auth_as):
        mfid = _instrument(client, db_engine, auth_as)
        auth_as("instr-admin", groups=[("instr-admin", _group_id_session(db_engine, "instr-admin"), Role.OWNER)])
        created = _create_deletion_request(client, mfid)
        return mfid, created["id"]

    def test_pending_project_hidden_from_list_search_and_exact_lookup(
        self, client, db_engine, auth_as, monkeypatch
    ):
        project_mfid, request_id = self._pending_project(client, db_engine, auth_as)
        auth_as("pending-owner-1", groups=[])

        assert client.get("/projects", params={"project_id": "proj_pending_1"}).json()["total"] == 0
        pending_detail = client.get(f"/projects/{project_mfid}")
        assert pending_detail.status_code == 200
        assert pending_detail.json()["deletion_request"]["status"] == "pending"
        assert pending_detail.json()["deletion_request"]["resource_id"] == project_mfid
        assert pending_detail.json()["deletion_request"]["resource_mfid"] == project_mfid

        from sqlalchemy import func, or_

        from app.api.routes import projects as projects_routes

        def project_search(query, tokens, fields, prefix_fields=()):
            return query.where(or_(
                *(func.lower(field).contains(token) for token in tokens for field in fields)
            ))

        monkeypatch.setattr(projects_routes, "configure_trigram_search", lambda s, t: None)
        monkeypatch.setattr(projects_routes, "apply_trigram_search", project_search)
        monkeypatch.setattr(projects_routes, "trigram_rank", lambda q, fields: literal(1))

        search = client.get("/projects/search", params={"q": "proj_pending"}).json()
        assert search["items"] == []

        auth_as("pending-owner-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])
        resp = client.patch(f"/deletion_requests/{request_id}", params={"status": "rejected"})
        assert resp.status_code == 200, resp.text

        auth_as("pending-owner-1", groups=[])

        assert client.get("/projects", params={"project_id": "proj_pending_1"}).json()["total"] == 1
        assert client.get(f"/projects/{project_mfid}").status_code == 200

        search = client.get("/projects/search", params={"q": "proj_pending"}).json()
        assert [item["project_id"] for item in search["items"]] == ["proj_pending_1"]

    def test_pending_instrument_hidden_from_list_search_and_exact_lookup(
        self, client, db_engine, auth_as, monkeypatch
    ):
        mfid, request_id = self._pending_instrument(client, db_engine, auth_as)
        auth_as("instr-admin", groups=[])

        exact = client.get("/instruments", params={"instrument_id": "xrd-del-1"}).json()
        assert exact["items"] == []
        canonical = client.get(f"/instruments/{mfid}")
        assert canonical.status_code == 200
        assert canonical.json()["deletion_request"]["status"] == "pending"

        from sqlalchemy import func, or_

        from app.api.routes import instruments as instruments_routes

        def instrument_search(query, tokens, fields, prefix_fields=()):
            return query.where(or_(
                *(func.lower(field).contains(token) for token in tokens for field in fields)
            ))

        monkeypatch.setattr(instruments_routes, "configure_trigram_search", lambda s, t: None)
        monkeypatch.setattr(instruments_routes, "apply_trigram_search", instrument_search)
        monkeypatch.setattr(instruments_routes, "trigram_rank", lambda q, fields: literal(1))

        search = client.get("/instruments/search", params={"q": "XRD deletion"}).json()
        assert search["items"] == []

        auth_as("instr-admin", groups=[("instr-admin", _group_id_session(db_engine, "instr-admin"), Role.OWNER)])
        resp = client.patch(f"/deletion_requests/{request_id}", params={"status": "rejected"})
        assert resp.status_code == 200, resp.text

        auth_as("instr-admin", groups=[])

        exact = client.get("/instruments", params={"instrument_id": "xrd-del-1"}).json()
        assert len(exact["items"]) == 1

        search = client.get("/instruments/search", params={"q": "XRD deletion"}).json()
        assert [item["unique_id"] for item in search["items"]] == [mfid]

    def test_pending_project_rejects_new_datasets_samples_members_and_reassign(self, client, db_engine, auth_as):
        project_mfid, request_id = self._pending_project(client, db_engine, auth_as)
        _add_member(db_engine, project_mfid, "pending-contrib-1", Role.CONTRIBUTOR)
        with Session(db_engine) as session:
            project = session.exec(
                select(Project).where(Project.unique_id == project_mfid)).one()
            project_id = project.project_id

        auth_as("pending-contrib-1", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d", "project_id": project_id})
        assert resp.status_code == 409, resp.text

        resp = client.post("/samples", json={"sample_name": "s", "project_id": project_id})
        assert resp.status_code == 409, resp.text

        auth_as("pending-owner-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])
        resp = client.post(f"/projects/{project_mfid}/users/pending-contrib-1")
        assert resp.status_code == 409, resp.text

        with Session(db_engine) as session:
            reassign_user = _user(session, "reassign-owner")
            session.add(Resource(mfid="reassign-ds", resource_type="dataset"))
            session.commit()
            session.add(Dataset(unique_id="reassign-ds", dataset_name="d",
                                owner_orcid="reassign-owner"))
            session.commit()
            ag_id = _group_id(session, "reassign-owner")
            session.add(ResourceACL(
                mfid="reassign-ds", accessgroup_id=ag_id,
                read=True, write=True, permission=Role.OWNER.db_value))
            session.add(UserAccessGroupLink(
                user_id=reassign_user.id,
                accessgroup_id=_group_id(session, project_mfid),
                role=Role.CONTRIBUTOR.db_value,
            ))
            session.commit()

        auth_as("reassign-owner", groups=[
            ("reassign-owner", _group_id_session(db_engine, "reassign-owner"), Role.OWNER),
            (project_mfid, _group_id_session(db_engine, project_mfid), Role.CONTRIBUTOR),
        ])
        resp = client.post("/resources/reassign-ds/project", json={"project_id": project_id})
        assert resp.status_code == 409, resp.text

        auth_as("pending-owner-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])
        resp = client.patch(f"/deletion_requests/{request_id}", params={"status": "rejected"})
        assert resp.status_code == 200, resp.text

        auth_as("pending-contrib-1", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d", "project_id": project_id})
        assert resp.status_code == 200, resp.text

    def test_pending_instrument_rejects_dataset_assignment(self, client, db_engine, auth_as):
        mfid, request_id = self._pending_instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            _user(session, "pending-contrib-2")
            session.add(Resource(mfid="pending-ds", resource_type="dataset"))
            session.commit()
            session.add(Dataset(unique_id="pending-ds", dataset_name="d",
                                owner_orcid="pending-contrib-2"))
            session.commit()
            personal_ag = session.exec(
                select(AccessGroup).where(
                    AccessGroup.group_name == "pending-contrib-2")).one()
            session.add(ResourceACL(
                mfid="pending-ds", accessgroup_id=personal_ag.id,
                read=True, write=True, permission=Role.OWNER.db_value))
            inst_ag = session.exec(
                select(AccessGroup).where(AccessGroup.group_name == mfid)).one()
            contrib = session.exec(
                select(User).where(User.unique_id == "pending-contrib-2")).one()
            session.add(UserAccessGroupLink(
                user_id=contrib.id, accessgroup_id=inst_ag.id,
                role=Role.CONTRIBUTOR.db_value))
            session.commit()

        auth_as("pending-contrib-2", groups=[
            ("pending-contrib-2", _group_id_session(db_engine, "pending-contrib-2"), Role.OWNER),
            (mfid, _group_id_session(db_engine, mfid), Role.CONTRIBUTOR),
        ])
        resp = client.post("/datasets", json={"dataset_name": "d2", "instrument_id": "xrd-del-1"})
        assert resp.status_code == 409, resp.text

        resp = client.put("/datasets/pending-ds/instrument", json={"instrument_id": "xrd-del-1"})
        assert resp.status_code == 409, resp.text

        auth_as("instr-admin", groups=[("instr-admin", _group_id_session(db_engine, "instr-admin"), Role.OWNER)])
        resp = client.patch(f"/deletion_requests/{request_id}", params={"status": "rejected"})
        assert resp.status_code == 200, resp.text

        auth_as("pending-contrib-2", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d2", "instrument_id": "xrd-del-1"})
        assert resp.status_code == 200, resp.text


class TestCrossGrants:
    def test_project_group_grant_on_other_resource_is_removed_owner_keeps_theirs(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_cross_1", "cross-owner-1")
        _grant_on_other_resource(db_engine, project_mfid)
        with Session(db_engine) as session:
            other_group = session.exec(
                select(AccessGroup).where(
                    AccessGroup.group_name == "other-owner")).one()
            session.add(ResourceACL(
                mfid="other-ds", accessgroup_id=other_group.id,
                read=True, write=True, permission=Role.OWNER.db_value))
            session.commit()

        auth_as("cross-owner-1", groups=[], platform_role=PlatformRole.ADMIN)
        preview = client.delete(f"/resources/{project_mfid}", params={"force": "true"})
        grants = preview.json()["removed_grants"]
        assert grants == [
            {"resource_mfid": "other-ds", "resource_type": "dataset", "permission": "contributor"}
        ]

        client.delete(f"/resources/{project_mfid}", params={"force": "true", "confirm": "true"})

        with Session(db_engine) as session:
            remaining = session.exec(select(ResourceACL).where(
                ResourceACL.mfid == "other-ds")).all()
            assert len(remaining) == 1
            assert remaining[0].permission == Role.OWNER.db_value
            assert session.get(Resource, "other-ds") is not None


class TestPermissionMatrix:
    def test_contributor_cannot_request_project_deletion(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_perm_1", "perm-owner-1")
        _add_member(db_engine, project_mfid, "perm-contrib-1", Role.CONTRIBUTOR)
        auth_as("perm-contrib-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.CONTRIBUTOR)])

        resp = client.post(f"/resources/{project_mfid}/delete")
        assert resp.status_code == 403, resp.text

    def test_admin_can_request_project_deletion(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_perm_2", "perm-owner-2")
        _add_member(db_engine, project_mfid, "perm-admin-1", Role.ADMIN)
        auth_as("perm-admin-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.ADMIN)])

        created = _create_deletion_request(client, project_mfid)
        assert created["status"] == "pending"

    def test_editor_and_stranger_cannot_force_project_deletion(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_perm_3", "perm-owner-3")
        _add_member(db_engine, project_mfid, "perm-editor-1", Role.EDITOR)
        auth_as("perm-editor-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.EDITOR)])
        assert client.delete(
            f"/resources/{project_mfid}",
            params={"force": "true", "confirm": "true"}).status_code == 403

        auth_as("perm-stranger-1", groups=[])
        assert client.delete(
            f"/resources/{project_mfid}",
            params={"force": "true", "confirm": "true"}).status_code == 403

    def test_owner_and_platform_admin_can_force_project_deletion(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_perm_4", "perm-owner-4")
        auth_as("perm-owner-4", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])
        resp = client.delete(
            f"/resources/{project_mfid}",
            params={"force": "true", "confirm": "true"})
        assert resp.status_code == 200, resp.text

        project_mfid = _project(db_engine, "proj_perm_5", "perm-owner-5")
        auth_as("perm-admin-2", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.delete(
            f"/resources/{project_mfid}",
            params={"force": "true", "confirm": "true"})
        assert resp.status_code == 200, resp.text


class TestDatasetSampleForcedDeleteUnchanged:
    def test_dataset_forced_delete_needs_no_confirm(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "ds-owner-del")
            session.add(Resource(mfid="ds-del-1", resource_type="dataset"))
            session.commit()
            session.add(Dataset(unique_id="ds-del-1", dataset_name="d",
                                owner_orcid="ds-owner-del"))
            session.commit()
            ag_id = _group_id(session, "ds-owner-del")
            session.add(ResourceACL(
                mfid="ds-del-1", accessgroup_id=ag_id,
                read=True, write=True, permission=Role.OWNER.db_value))
            session.commit()

        auth_as("ds-owner-del", groups=[("ds-owner-del", _group_id_session(db_engine, "ds-owner-del"), Role.OWNER)])
        resp = client.delete("/resources/ds-del-1", params={"force": "true"})
        assert resp.status_code == 200, resp.text
        assert "permanently deleted" in resp.json()["detail"]

        with Session(db_engine) as session:
            assert session.get(Resource, "ds-del-1") is None

    def test_sample_forced_delete_needs_no_confirm(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "smp-owner-del")
            session.add(Resource(mfid="smp-del-1", resource_type="sample"))
            session.commit()
            session.add(Sample(unique_id="smp-del-1", sample_name="s",
                               owner_orcid="smp-owner-del"))
            session.commit()
            ag_id = _group_id(session, "smp-owner-del")
            session.add(ResourceACL(
                mfid="smp-del-1", accessgroup_id=ag_id,
                read=True, write=True, permission=Role.OWNER.db_value))
            session.commit()

        auth_as("smp-owner-del", groups=[("smp-owner-del", _group_id_session(db_engine, "smp-owner-del"), Role.OWNER)])
        resp = client.delete("/resources/smp-del-1", params={"force": "true"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            assert session.get(Resource, "smp-del-1") is None


class TestSlugReuse:
    def test_project_slug_is_reusable_after_deletion(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_slug_reuse", "slug-owner-1")
        auth_as("slug-owner-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.delete(
            f"/resources/{project_mfid}",
            params={"force": "true", "confirm": "true"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            _user(session, "slug-owner-2")
        auth_as("slug-owner-2", groups=[])
        resp = client.post("/projects", json={
            "project_id": "proj_slug_reuse",
            "organization": "LBNL",
            "project_lead_orcid": "slug-owner-2",
        })
        assert resp.status_code == 200, resp.text

    def test_instrument_slug_is_reusable_after_deletion(self, client, db_engine, auth_as):
        mfid = _instrument(client, db_engine, auth_as)
        auth_as("instr-admin", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.delete(
            f"/resources/{mfid}",
            params={"force": "true", "confirm": "true"})
        assert resp.status_code == 200, resp.text

        resp = client.post("/instruments", json=INSTRUMENT_PAYLOAD)
        assert resp.status_code == 200, resp.text


class TestMetadataAndCapability:
    def test_project_deletion_removes_scientific_metadata(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_md_1", "md-owner-1")
        with Session(db_engine) as session:
            session.add(ScientificMetadataBinary(
                unique_id=project_mfid, scientific_metadata={"a": 1}))
            session.commit()

        auth_as("md-owner-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.delete(
            f"/resources/{project_mfid}",
            params={"force": "true", "confirm": "true"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            assert session.exec(
                select(ScientificMetadataBinary).where(
                    ScientificMetadataBinary.unique_id == project_mfid)
            ).all() == []

    def test_empty_project_can_delete_capability_for_admin(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_cap_1", "cap-owner-1")
        auth_as("cap-owner-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])

        body = client.get(f"/projects/{project_mfid}").json()
        assert body["capabilities"]["can_delete"] is True

    def test_owner_sees_can_delete_false_once_a_sample_is_assigned(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_cap_3", "cap-owner-3")
        with Session(db_engine) as session:
            session.add(Sample(unique_id="proj_cap_3-smp", sample_name="s",
                               owner_orcid="cap-owner-3", project_mfid=project_mfid))
            session.commit()
        auth_as("cap-owner-3", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.OWNER)])

        body = client.get(f"/projects/{project_mfid}").json()
        assert body["capabilities"]["can_delete"] is False

    def test_contributor_sees_can_delete_false(self, client, db_engine, auth_as):
        project_mfid = _project(db_engine, "proj_cap_2", "cap-owner-2")
        _add_member(db_engine, project_mfid, "cap-contrib-1", Role.CONTRIBUTOR)
        auth_as("cap-contrib-1", groups=[(project_mfid, _group_id_session(db_engine, project_mfid), Role.CONTRIBUTOR)])

        body = client.get(f"/projects/{project_mfid}").json()
        assert body["capabilities"]["can_delete"] is False

    def test_instrument_can_delete_for_owner(self, client, db_engine, auth_as):
        mfid = _instrument(client, db_engine, auth_as)
        auth_as("instr-admin", groups=[("instr-admin", _group_id_session(db_engine, "instr-admin"), Role.OWNER)])

        body = client.get(f"/instruments/{mfid}").json()
        assert body["capabilities"]["can_delete"] is True

    def test_dataset_capability_stays_false(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "ds-cap-owner")
            session.add(Resource(mfid="ds-cap-1", resource_type="dataset"))
            session.commit()
            session.add(Dataset(unique_id="ds-cap-1", dataset_name="d",
                                owner_orcid="ds-cap-owner"))
            session.commit()
            ag_id = _group_id(session, "ds-cap-owner")
            session.add(ResourceACL(
                mfid="ds-cap-1", accessgroup_id=ag_id,
                read=True, write=True, permission=Role.OWNER.db_value))
            session.commit()

        auth_as("ds-cap-owner", groups=[("ds-cap-owner", _group_id_session(db_engine, "ds-cap-owner"), Role.OWNER)])
        body = client.get("/datasets/ds-cap-1").json()
        assert body["capabilities"]["can_delete"] is False


class TestDeleteCapabilityCheck:
    def test_an_instrument_with_datasets_cannot_be_deleted(self, client, db_engine, auth_as):
        mfid = _instrument(client, db_engine, auth_as)
        with Session(db_engine) as session:
            session.add(Dataset(unique_id="instr-cap-ds", dataset_name="d", instrument_mfid=mfid))
            session.add(Resource(mfid="instr-cap-ds", resource_type="dataset"))
            session.commit()
        auth_as("instr-admin", groups=[], platform_role=PlatformRole.ADMIN)
        assert client.get(f"/instruments/{mfid}").json()["capabilities"]["can_delete"] is False
        assert client.get(f"/resources/{mfid}").json()["capabilities"]["can_delete"] is False

    def test_callers_who_cannot_delete_trigger_no_blocker_queries(self, client, db_engine, auth_as, monkeypatch):
        import app.api.routes.projects as projects_routes
        import app.services.resource_details as resource_details
        calls = []
        def counting(*args):
            calls.append(args)
            return False
        monkeypatch.setattr(projects_routes, "has_deletion_blockers", counting)
        monkeypatch.setattr(resource_details, "has_deletion_blockers", counting)
        project_mfid = _project(db_engine, "proj_cap_lazy", "cap-owner-lazy")
        _add_member(db_engine, project_mfid, "cap-viewer-lazy", Role.VIEWER)
        group_id = _group_id_session(db_engine, project_mfid)
        auth_as("cap-viewer-lazy", groups=[(project_mfid, group_id, Role.VIEWER)])
        client.get(f"/projects/{project_mfid}")
        client.get(f"/resources/{project_mfid}")
        assert calls == []
        auth_as("cap-owner-lazy", groups=[(project_mfid, group_id, Role.OWNER)])
        assert client.get(f"/projects/{project_mfid}").json()["capabilities"]["can_delete"] is True
        assert len(calls) == 1
