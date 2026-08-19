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
        """The admin bypass is for standing verification, not referential
        integrity — the named project still has to be real."""
        with Session(db_engine) as session:
            _user(session, "0000-0007")
            _project_with_group(session, "proj_admin_bypass", "0000-0007")

        auth_as("sa-admin", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/datasets", json={
            "dataset_name": "d7", "project_id": "proj_admin_bypass", "owner_orcid": "0000-0007",
        })
        assert resp.status_code == 200, resp.text

    def test_nonexistent_project_id_is_404_even_for_platform_admin(self, client, db_engine, auth_as):
        """project_mfid is a real FK now — a bogus project_id can't slip
        through even under the admin standing-check bypass."""
        with Session(db_engine) as session:
            _user(session, "0000-0008x")

        auth_as("sa-admin-2", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/datasets", json={
            "dataset_name": "d7b", "project_id": "no_such_project", "owner_orcid": "0000-0008x",
        })
        assert resp.status_code == 404

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

    def test_create_sample_with_project_id_sets_project_mfid(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            researcher = _user(session, "0000-0012b")
            ag = _project_with_group(session, "proj_mfid_s", "0000-0012b")
            session.add(UserAccessGroupLink(user_id=researcher.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()

        auth_as("0000-0012b", groups=[])
        resp = client.post("/samples", json={"sample_name": "s2b", "project_id": "proj_mfid_s"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            samp = session.exec(select(Sample).where(Sample.sample_name == "s2b")).one()
            proj = session.exec(select(Project).where(Project.project_id == "proj_mfid_s")).one()
            assert samp.project_mfid == proj.unique_id

    def test_create_sample_with_nonexistent_project_id_is_404_for_platform_admin(self, client, db_engine, auth_as):
        auth_as("sa-admin-3", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/samples", json={"sample_name": "s2c", "project_id": "no-such-project"})
        assert resp.status_code == 404

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


class TestCreateOnBehalfByIdentifier:
    """The owner field is the flexible superset of owner_orcid: same
    create-on-behalf authorization, but accepts an ORCID, username, or
    email, resolved server-side via resolve_user_by_identifier."""

    def test_dataset_owner_by_email_resolves_and_checks_standing(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            researcher = _user(session, "0000-0021")
            ag = _project_with_group(session, "proj_id_1", "0000-0021")
            session.add(UserAccessGroupLink(user_id=researcher.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()

        auth_as("sa-instrument-10", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d20", "project_id": "proj_id_1", "owner": "0000-0021@x.com",
        })
        assert resp.status_code == 200, resp.text
        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d20")).one()
            assert ds.owner_orcid == "0000-0021"

    def test_dataset_owner_by_username_resolves(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            researcher = User(first_name="U", last_name="Name", email="uname@x.com",
                              unique_id="0000-0022", username="dsowner")
            session.add(researcher)
            ag = _project_with_group(session, "proj_id_2", "0000-0022")
            session.commit()
            session.refresh(researcher)
            session.add(UserAccessGroupLink(user_id=researcher.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()

        auth_as("sa-instrument-11", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d21", "project_id": "proj_id_2", "owner": "dsowner",
        })
        assert resp.status_code == 200, resp.text
        with Session(db_engine) as session:
            ds = session.exec(select(Dataset).where(Dataset.dataset_name == "d21")).one()
            assert ds.owner_orcid == "0000-0022"

    def test_owner_without_standing_is_still_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0023")
            _project_with_group(session, "proj_id_3", "0000-0023")
            session.commit()

        auth_as("sa-instrument-12", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d22", "project_id": "proj_id_3", "owner": "0000-0023@x.com",
        })
        assert resp.status_code == 403

    def test_owner_and_owner_orcid_together_is_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0024")

        auth_as("sa-instrument-13", groups=[])
        resp = client.post("/datasets", json={
            "dataset_name": "d23", "owner": "0000-0024@x.com", "owner_orcid": "0000-0024",
        })
        assert resp.status_code == 400

    def test_unresolvable_owner_is_404(self, client, db_engine, auth_as):
        auth_as("sa-instrument-14", groups=[], is_service_account=True)
        resp = client.post("/datasets", json={"dataset_name": "d24", "owner": "nobody@x.com"})
        assert resp.status_code == 404

    def test_sample_owner_by_email_resolves(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            researcher = _user(session, "0000-0025")
            ag = _project_with_group(session, "proj_id_4", "0000-0025")
            session.add(UserAccessGroupLink(user_id=researcher.id, accessgroup_id=ag.id, role="contributor"))
            session.commit()

        auth_as("sa-instrument-15", groups=[])
        resp = client.post("/samples", json={
            "sample_name": "s20", "project_id": "proj_id_4", "owner": "0000-0025@x.com",
        })
        assert resp.status_code == 200, resp.text
        with Session(db_engine) as session:
            samp = session.exec(select(Sample).where(Sample.sample_name == "s20")).one()
            assert samp.owner_orcid == "0000-0025"

    def test_sample_owner_and_owner_orcid_together_is_rejected(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0026")

        auth_as("sa-instrument-16", groups=[])
        resp = client.post("/samples", json={
            "sample_name": "s21", "owner": "0000-0026@x.com", "owner_orcid": "0000-0026",
        })
        assert resp.status_code == 400
