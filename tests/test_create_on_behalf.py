"""POST /datasets and POST /samples must verify the effective owner (not
necessarily the caller) has >= contributor standing in the target project.
Before this fix, owner_orcid arrived in the request body and was used
unverified — any authenticated caller could attribute a resource to anyone,
in any project.
"""
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Dataset, Project, Sample, User, UserAccessGroupLink
from app.utils.permissions import PlatformRole, Role


def _user(session, orcid):
    u = User(first_name="T", last_name=orcid[-4:], email=f"{orcid}@x.com", unique_id=orcid)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _project_with_group(session, proj_id, lead_orcid):
    proj = Project(project_id=proj_id, organization="LBNL", project_lead_orcid=lead_orcid)
    session.add(proj)
    session.commit()
    session.refresh(proj)
    ag = AccessGroup(group_name=proj.unique_id)
    session.add(ag)
    session.commit()
    session.refresh(ag)
    return ag


class TestCreateDataset:
    def test_self_service_with_standing_succeeds(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            caller = _user(session, "0000-0001")
            ag = _project_with_group(session, "proj_x", "0000-0001")
            session.add(UserAccessGroupLink(user_id=caller.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()
            ag_id = ag.id

        auth_as("0000-0001", groups=[("proj_x", ag_id, Role.CONTRIBUTOR)])
        resp = client.post("/datasets", json={"dataset_name": "d1", "project_id": "proj_x"})
        assert resp.status_code == 200, resp.text

    def test_self_service_without_standing_is_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0002")
            ag = _project_with_group(session, "proj_y", "0000-0002")
            session.commit()
            ag_id = ag.id

        auth_as("0000-0002", groups=[("proj_y", ag_id, Role.VIEWER)])
        resp = client.post("/datasets", json={"dataset_name": "d2", "project_id": "proj_y"})
        assert resp.status_code == 403

    def test_create_on_behalf_checks_the_named_owner_not_the_caller(self, client, db_engine, auth_as):
        """The instrument SA workflow: the caller (SA) needs no standing in
        the project at all — only the named owner's standing is checked."""
        with Session(db_engine) as session:
            researcher = _user(session, "0000-0003")
            ag = _project_with_group(session, "proj_z", "0000-0003")
            session.add(UserAccessGroupLink(user_id=researcher.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()

        auth_as("sa-instrument-1", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d3", "project_id": "proj_z", "owner_orcid": "0000-0003",
        })
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d3")).one()
            assert ds.owner_orcid == "0000-0003"

    def test_create_on_behalf_of_a_non_member_is_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0004")
            _project_with_group(session, "proj_w", "0000-0004")
            session.commit()

        auth_as("sa-instrument-2", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d4", "project_id": "proj_w", "owner_orcid": "0000-0004",
        })
        assert resp.status_code == 403

    def test_no_project_and_different_owner_is_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0005")

        auth_as("sa-instrument-3", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d5", "owner_orcid": "0000-0005"})
        assert resp.status_code == 400

    def test_no_project_self_owned_is_allowed(self, client, db_engine, auth_as):
        auth_as("0000-0006", groups=[])
        resp = client.post("/datasets", json={"dataset_name": "d6"})
        assert resp.status_code == 200, resp.text

    def test_platform_admin_bypasses_the_check(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0007")

        auth_as("sa-admin", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/datasets", json={
            "dataset_name": "d7", "project_id": "no_such_project", "owner_orcid": "0000-0007",
        })
        assert resp.status_code == 200, resp.text

    def test_service_account_no_project_and_different_owner_is_allowed(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0008")

        auth_as("sa-instrument-5", groups=[], is_service_account=True)
        resp = client.post("/datasets", json={"dataset_name": "d8", "owner_orcid": "0000-0008"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d8")).one()
            assert ds.owner_orcid == "0000-0008"

    def test_service_account_no_project_and_nonexistent_owner_is_rejected(self, client, db_engine, auth_as):
        auth_as("sa-instrument-6", groups=[], is_service_account=True)
        resp = client.post("/datasets", json={"dataset_name": "d9", "owner_orcid": "0000-0009"})
        assert resp.status_code == 404


class TestCreateSample:
    def test_self_service_without_standing_is_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0011")
            ag = _project_with_group(session, "proj_s", "0000-0011")
            session.commit()
            ag_id = ag.id

        auth_as("0000-0011", groups=[("proj_s", ag_id, Role.VIEWER)])
        resp = client.post("/samples", json={"sample_name": "s1", "project_id": "proj_s"})
        assert resp.status_code == 403

    def test_create_on_behalf_checks_the_named_owner(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            researcher = _user(session, "0000-0012")
            ag = _project_with_group(session, "proj_t", "0000-0012")
            session.add(UserAccessGroupLink(user_id=researcher.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()

        auth_as("sa-instrument-4", groups=[])
        resp = client.post("/samples", json={
            "sample_name": "s2", "project_id": "proj_t", "owner_orcid": "0000-0012",
        })
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            samp = session.exec(select(Sample).where(Sample.sample_name == "s2")).one()
            assert samp.owner_orcid == "0000-0012"

    def test_no_project_and_different_owner_is_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0013")

        auth_as("sa-instrument-7", groups=[])
        resp = client.post("/samples", json={"sample_name": "s3", "owner_orcid": "0000-0013"})
        assert resp.status_code == 400

    def test_service_account_no_project_and_different_owner_is_allowed(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0014")

        auth_as("sa-instrument-8", groups=[], is_service_account=True)
        resp = client.post("/samples", json={"sample_name": "s4", "owner_orcid": "0000-0014"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            samp = session.exec(select(Sample).where(Sample.sample_name == "s4")).one()
            assert samp.owner_orcid == "0000-0014"

    def test_service_account_no_project_and_nonexistent_owner_is_rejected(self, client, db_engine, auth_as):
        auth_as("sa-instrument-9", groups=[], is_service_account=True)
        resp = client.post("/samples", json={"sample_name": "s5", "owner_orcid": "0000-0015"})
        assert resp.status_code == 404
