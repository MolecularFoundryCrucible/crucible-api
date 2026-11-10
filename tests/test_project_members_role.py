"""Project members and their roles come from the project's access group."""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.tables import AccessGroup, Project, ResourceACL, User, UserAccessGroupLink
from app.api.routes.projects import _get_project_users
from app.utils.permissions import Principal
from app.tables import UserIdentity


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[
        AccessGroup.__table__, Project.__table__, User.__table__, UserIdentity.__table__,
        UserAccessGroupLink.__table__, ResourceACL.__table__,
    ])
    return engine


def _project(session, project_id, lead_orcid):
    proj = Project(project_id=project_id, organization="LBNL", project_lead_orcid=lead_orcid)
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


def test_member_role_comes_from_the_project_access_group():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_x", "0000-0001")
        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        lead = User(first_name="A", last_name="Lead", email="a@x.com", unique_id="0000-0001")
        member = User(first_name="B", last_name="Member", email="b@x.com", unique_id="0000-0002")
        session.add(lead)
        session.add(member)
        session.commit()
        session.refresh(ag)
        session.refresh(lead)
        session.refresh(member)

        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=ag.id, role="owner"))
        session.add(UserAccessGroupLink(user_id=member.id, accessgroup_id=ag.id, role="contributor"))
        session.commit()

        result = {u.unique_id: u.role for u in _get_project_users(proj, session, Principal(user_id="0000-0001"))}
        assert result == {"0000-0001": "owner", "0000-0002": "contributor"}


def test_access_group_link_with_null_role_is_rejected():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_y", "0000-0003")
        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        user = User(first_name="C", last_name="Unbackfilled", email="c@x.com", unique_id="0000-0003")
        session.add(user)
        session.commit()
        session.refresh(ag)
        session.refresh(user)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role=None))
        with pytest.raises(IntegrityError):
            session.commit()
