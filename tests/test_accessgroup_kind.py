"""AccessGroup.owner_orcid / resource_mfid classify a group by kind instead
of it being inferred from group_name. Personal ORCID groups get owner_orcid;
a project's or instrument's own group gets resource_mfid. Mutually exclusive,
enforced by a CHECK constraint.
"""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models.core_models import AccessGroup, Project, User
from app.utils.permissions import PlatformRole
from app.utils.user_utils import sql_add_user_to_access_group


def _user(session, orcid):
    u = User(first_name="T", last_name=orcid[-4:], email=f"{orcid}@x.com", unique_id=orcid)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


class TestSqlAddUserToAccessGroup:
    def test_personal_group_gets_owner_orcid(self, db_engine):
        with Session(db_engine) as session:
            _user(session, "0000-0001")
            ag = sql_add_user_to_access_group("0000-0001", "0000-0001", session)
            session.commit()
            assert ag.owner_orcid == "0000-0001"
            assert ag.resource_mfid is None

    def test_project_membership_does_not_get_owner_orcid(self, db_engine):
        with Session(db_engine) as session:
            _user(session, "0000-0002")
            ag = sql_add_user_to_access_group("0000-0002", "proj_x", session)
            session.commit()
            assert ag.owner_orcid is None
            assert ag.resource_mfid is None

    def test_does_not_overwrite_an_already_classified_group(self, db_engine):
        with Session(db_engine) as session:
            _user(session, "0000-0003")
            session.add(AccessGroup(group_name="0000-0003", resource_mfid="some-resource"))
            session.commit()
            ag = sql_add_user_to_access_group("0000-0003", "0000-0003", session)
            session.commit()
            assert ag.resource_mfid == "some-resource"
            assert ag.owner_orcid is None


class TestCreationRoutesClassifyTheirGroup:
    def test_create_project_sets_resource_mfid(self, client, db_engine, auth_as):
        with Session(db_engine) as session:
            _user(session, "0000-0004")

        auth_as("0000-0004", groups=[])
        resp = client.post("/projects", json={
            "project_id": "proj_kind", "organization": "LBNL", "project_lead_orcid": "0000-0004",
        })
        assert resp.status_code == 200, resp.text

        with Session(db_engine) as session:
            proj = session.exec(select(Project).where(Project.project_id == "proj_kind")).one()
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == proj.unique_id)).one()
            assert ag.resource_mfid == "proj_kind"
            assert ag.owner_orcid is None

    def test_create_instrument_sets_resource_mfid(self, client, db_engine, auth_as):
        auth_as("admin-1", groups=[], platform_role=PlatformRole.ADMIN)
        resp = client.post("/instruments", json={
            "instrument_id": "xrd-k", "instrument_name": "XRD-K", "owner": "facility", "location": "b30",
        })
        assert resp.status_code == 200, resp.text
        mfid = resp.json()["unique_id"]

        with Session(db_engine) as session:
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == mfid)).one()
            assert ag.resource_mfid == mfid
            assert ag.owner_orcid is None


def test_kind_columns_are_mutually_exclusive(db_engine):
    with Session(db_engine) as session:
        session.add(AccessGroup(group_name="both", owner_orcid="0000-0005", resource_mfid="something"))
        with pytest.raises(IntegrityError):
            session.commit()
