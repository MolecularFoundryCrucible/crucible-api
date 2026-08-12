"""POST /resources/{mfid}/transfer_ownership.

Two resource shapes, covered separately: a project (one ACL entry, ever;
ownership is standing within its own group) and a dataset (ownership is an
ACL row on the owner's personal ORCID group). Both demote the previous owner
to admin rather than removing them, and both default to a no-op preview
unless ?confirm=true.
"""
from sqlmodel import Session, select

from app.models.core_models import (
    AccessGroup, Dataset, Project, Resource, ResourceACL, User, UserAccessGroupLink)
from app.utils.permissions import PlatformRole, Role


def _user(session, orcid, username=None, email=None):
    u = User(first_name="T", last_name=orcid[-4:], email=email or f"{orcid}@x.com",
            unique_id=orcid, username=username)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _group(session, name):
    ag = AccessGroup(group_name=name)
    session.add(ag)
    session.commit()
    session.refresh(ag)
    return ag


class TestProjectTransfer:
    def _setup(self, db_engine):
        with Session(db_engine) as session:
            old_owner = _user(session, "0000-0001")
            _user(session, "0000-0002", username="newlead")
            proj = Project(project_id="proj_x", organization="LBNL", project_lead_orcid="0000-0001")
            session.add(proj)
            session.commit()
            session.refresh(proj)
            proj_ag = _group(session, proj.unique_id)
            _group(session, "0000-0002")  # new owner's personal group must exist
            session.add(Resource(mfid="proj_x", resource_type="project"))
            session.add(UserAccessGroupLink(user_id=old_owner.id, accessgroup_id=proj_ag.id, role="owner"))
            session.commit()
            # A project's one and only ACL entry: its own group, at owner.
            session.add(ResourceACL(mfid="proj_x", accessgroup_id=proj_ag.id, read=True, write=True, role="owner"))
            session.commit()
            return proj_ag.id

    def test_preview_does_not_mutate_anything(self, client, db_engine, auth_as):
        ag_id = self._setup(db_engine)
        auth_as("0000-0001", groups=[("proj_x", ag_id, Role.OWNER)])

        resp = client.post("/resources/proj_x/transfer_ownership", json={"new_owner": "newlead"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["previous_owner"]["unique_id"] == "0000-0001"
        assert body["new_owner"]["unique_id"] == "0000-0002"

        with Session(db_engine) as session:
            link = session.exec(
                select(UserAccessGroupLink).where(UserAccessGroupLink.accessgroup_id == ag_id)
            ).one()
            assert link.role == "owner"  # unchanged — preview must not write
            proj = session.exec(select(Project).where(Project.project_id == "proj_x")).one()
            assert proj.project_lead_orcid == "0000-0001"  # unchanged

    def test_confirm_transfers_standing_and_syncs_lead_pointer(self, client, db_engine, auth_as):
        ag_id = self._setup(db_engine)
        auth_as("0000-0001", groups=[("proj_x", ag_id, Role.OWNER)])

        resp = client.post("/resources/proj_x/transfer_ownership?confirm=true",
                           json={"new_owner": "newlead"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            links = {
                l.user_id: l.role for l in session.exec(
                    select(UserAccessGroupLink).where(UserAccessGroupLink.accessgroup_id == ag_id)
                ).all()
            }
            old_owner = session.exec(select(User).where(User.unique_id == "0000-0001")).one()
            new_owner = session.exec(select(User).where(User.unique_id == "0000-0002")).one()
            assert links[old_owner.id] == "admin"     # demoted, not removed
            assert links[new_owner.id] == "owner"

            proj = session.exec(select(Project).where(Project.project_id == "proj_x")).one()
            assert proj.project_lead_orcid == "0000-0002"

    def test_non_owner_cannot_transfer(self, client, db_engine, auth_as):
        ag_id = self._setup(db_engine)
        auth_as("0000-0001", groups=[("proj_x", ag_id, Role.ADMIN)])

        resp = client.post("/resources/proj_x/transfer_ownership?confirm=true",
                           json={"new_owner": "newlead"})
        assert resp.status_code == 403


class TestDatasetTransfer:
    def _setup(self, db_engine):
        with Session(db_engine) as session:
            old_owner = _user(session, "0000-0011")
            _user(session, "0000-0012", username="newowner")
            old_ag = _group(session, "0000-0011")
            new_ag = _group(session, "0000-0012")
            session.add(Resource(mfid="ds_1", resource_type="dataset"))
            session.add(Dataset(unique_id="ds_1", dataset_name="d", owner_orcid="0000-0011",
                                owner_user_id=old_owner.id))
            session.commit()
            session.add(ResourceACL(mfid="ds_1", accessgroup_id=old_ag.id, read=True, write=True, role="owner"))
            session.commit()
            return old_ag.id, new_ag.id

    def test_confirm_moves_the_acl_row_and_syncs_owner_columns(self, client, db_engine, auth_as):
        """The old owner's personal-group grant is removed outright, not
        demoted — unlike a project, leaving "Alice's personal group is admin
        here" behind doesn't fit what a personal group means."""
        old_ag_id, new_ag_id = self._setup(db_engine)
        auth_as("0000-0011", groups=[("0000-0011", old_ag_id, Role.OWNER)])

        resp = client.post("/resources/ds_1/transfer_ownership?confirm=true",
                           json={"new_owner": "newowner"})
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            acls = {a.accessgroup_id: a.role for a in
                   session.exec(select(ResourceACL).where(ResourceACL.mfid == "ds_1")).all()}
            assert old_ag_id not in acls
            assert acls[new_ag_id] == "owner"

            ds = session.exec(select(Dataset).where(Dataset.unique_id == "ds_1")).one()
            assert ds.owner_orcid == "0000-0012"

    def test_new_owner_without_a_personal_group_is_rejected(self, client, db_engine, auth_as):
        """resolve_user_by_identifier finds the user, but they have no
        AccessGroup — a state that shouldn't exist for a real registered
        user, but must fail loudly rather than silently grant nothing."""
        with Session(db_engine) as session:
            old_owner = _user(session, "0000-0021")
            _user(session, "0000-0022")  # exists, but no AccessGroup for them
            old_ag = _group(session, "0000-0021")
            session.add(Resource(mfid="ds_2", resource_type="dataset"))
            session.add(Dataset(unique_id="ds_2", dataset_name="d", owner_orcid="0000-0021",
                                owner_user_id=old_owner.id))
            session.commit()
            session.add(ResourceACL(mfid="ds_2", accessgroup_id=old_ag.id, read=True, write=True, role="owner"))
            session.commit()
            old_ag_id = old_ag.id

        auth_as("0000-0021", groups=[("0000-0021", old_ag_id, Role.OWNER)])
        resp = client.post("/resources/ds_2/transfer_ownership?confirm=true",
                           json={"new_owner": "0000-0022@x.com"})
        assert resp.status_code == 409
