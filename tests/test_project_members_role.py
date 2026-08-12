"""_get_project_users must report each member's role from the project's own
access group. UserAccessGroupLink is authoritative for membership — not
UserProjectLink, which can drift from it (e.g. via the generic
POST/DELETE /users/{orcid}/access_groups/{group_name} routes, which never
touch UserProjectLink). See is_project_member in app/utils/project_utils.py.

The access group is keyed by the project's MFID (Project.unique_id), not its
project_id — see docs/permission_system_plan.md, "Groups".
"""
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import AccessGroup, Project, User, UserAccessGroupLink, UserProjectLink
from app.api.routes.projects import _get_project_users


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[
        AccessGroup.__table__, Project.__table__, User.__table__,
        UserAccessGroupLink.__table__, UserProjectLink.__table__,
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

        session.add(UserProjectLink(user_id=lead.id, project_id="proj_x"))
        session.add(UserProjectLink(user_id=member.id, project_id="proj_x"))
        session.add(UserAccessGroupLink(user_id=lead.id, accessgroup_id=ag.id, role="owner"))
        session.add(UserAccessGroupLink(user_id=member.id, accessgroup_id=ag.id, role="contributor"))
        session.commit()

        result = {u.unique_id: u.role for u in _get_project_users("proj_x", session)}
        assert result == {"0000-0001": "owner", "0000-0002": "contributor"}


def test_access_group_link_with_null_role_reports_unknown_not_a_crash():
    """A pre-backfill UserAccessGroupLink row (role never set) is stale data,
    not a reason to 500 the route."""
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
        session.commit()

        result = _get_project_users("proj_y", session)
        assert len(result) == 1
        assert result[0].role == "unknown"


def test_stale_user_project_link_with_no_real_access_is_not_a_member():
    """The whole point of reading UserAccessGroupLink instead of
    UserProjectLink: a membership row left over after real access was revoked
    through the generic access-group routes must not still count as a member."""
    engine = _engine()
    with Session(engine) as session:
        _project(session, "proj_z", "0000-0004")
        user = User(first_name="D", last_name="StaleLink", email="d@x.com", unique_id="0000-0004")
        session.add(user)
        session.commit()
        session.refresh(user)

        session.add(UserProjectLink(user_id=user.id, project_id="proj_z"))
        session.commit()

        assert _get_project_users("proj_z", session) == []
