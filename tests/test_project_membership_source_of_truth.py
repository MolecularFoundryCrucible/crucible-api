"""is_project_member and is_project_group must read UserAccessGroupLink, not
UserProjectLink — see docs/permission_system_plan.md, "table-wise, is our
handling of project membership correct?" UserProjectLink has a real FK to
`project` but is never consulted for authorization; UserAccessGroupLink is
what authorize_user actually builds a Principal's groups from. The two drift
apart via the generic POST/DELETE /users/{orcid}/access_groups/{group_name}
routes, which only ever touch UserAccessGroupLink.

The access group is keyed by the project's MFID (Project.unique_id), not its
project_id — see docs/permission_system_plan.md, "Groups".
"""
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import AccessGroup, Project, User, UserAccessGroupLink, UserProjectLink
from app.utils.project_utils import is_project_group, is_project_member, sql_get_projects_for_user


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[
        AccessGroup.__table__, Project.__table__, User.__table__,
        UserAccessGroupLink.__table__, UserProjectLink.__table__,
    ])
    return engine


def _user(session, orcid):
    u = User(first_name="T", last_name="User", email=f"{orcid}@x.com", unique_id=orcid)
    session.add(u)
    session.commit()
    session.refresh(u)
    return u


def _project(session, project_id, lead_orcid):
    proj = Project(project_id=project_id, organization="org", project_lead_orcid=lead_orcid)
    session.add(proj)
    session.commit()
    session.refresh(proj)
    return proj


def test_real_access_group_standing_is_membership():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_a", "0000-0010")
        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        user = _user(session, "0000-0010")
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="contributor"))
        session.commit()

        assert is_project_member(user.id, "proj_a", session) is True


def test_user_project_link_alone_is_not_membership():
    """The exact drift scenario: granted through the generic access-group
    route's opposite (revoked there), UserProjectLink survives with nothing
    backing it."""
    engine = _engine()
    with Session(engine) as session:
        _project(session, "proj_b", "0000-0011")
        user = _user(session, "0000-0011")
        session.add(UserProjectLink(user_id=user.id, project_id="proj_b"))
        session.commit()

        assert is_project_member(user.id, "proj_b", session) is False


def test_access_group_standing_without_user_project_link_is_still_membership():
    """The other half of the drift: granted via the generic route, never
    reflected in UserProjectLink. Real access must still count."""
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_c", "0000-0012")
        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        user = _user(session, "0000-0012")
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="viewer"))
        session.commit()

        assert is_project_member(user.id, "proj_c", session) is True


def test_sql_get_projects_for_user_matches_is_project_member():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_d", "0000-0013")
        ag = AccessGroup(group_name=proj.unique_id)
        session.add(ag)
        user = _user(session, "0000-0013")
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="owner"))
        session.commit()

        projects = sql_get_projects_for_user("0000-0013", session)
        assert [p.project_id for p in projects] == ["proj_d"]


def test_is_project_group_distinguishes_projects_from_other_groups():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "real_proj", "0000-0014")

        assert is_project_group(proj.unique_id, session) is True
        assert is_project_group("0000-0014", session) is False  # an ORCID group
        assert is_project_group("some-instrument", session) is False
