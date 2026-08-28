"""is_project_owner is the real "project lead" check now — Project.
project_lead_orcid is a denormalized display pointer, not read for
authorization (see docs/permission_system_plan.md, "Project lead becomes a
role"). The one subtlety: it must confirm proj_id is an actual project before
checking standing, since owner standing in a caller's own ORCID group is
trivially true for everyone and would otherwise let `?group_name=<my orcid>`
impersonate project-lead authorization in access_groups.py's join-request
routes.

The access group is keyed by the project's MFID (Project.unique_id), not its
project_id — see docs/permission_system_plan.md, "Groups".
"""
from sqlmodel import Session, SQLModel, create_engine, select
from sqlalchemy.pool import StaticPool

from app.models.core_models import AccessGroup, Project, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType
from app.utils.project_utils import is_project_owner


def _engine():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine, tables=[
        AccessGroup.__table__, Project.__table__, User.__table__, UserAccessGroupLink.__table__,
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


def test_owner_standing_in_the_project_group_is_the_lead():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_a", "0000-0001")
        ag = AccessGroup(group_name=proj.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add(ag)
        user = _user(session, "0000-0001")
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="owner"))
        session.commit()

        assert is_project_owner("0000-0001", "proj_a", session) is True


def test_admin_standing_is_not_ownership():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_b", "0000-0002")
        ag = AccessGroup(group_name=proj.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add(ag)
        user = _user(session, "0000-0003")
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="admin"))
        session.commit()

        assert is_project_owner("0000-0003", "proj_b", session) is False


def test_owner_of_your_own_orcid_group_does_not_make_you_the_lead_of_an_unrelated_name():
    """The exact bug this closes: everyone is owner in their own ORCID group,
    so a naive standing check on an arbitrary group_name would trivially
    pass whenever group_name happens to equal the caller's own ORCID."""
    engine = _engine()
    with Session(engine) as session:
        orcid = "0000-0004"
        ag = AccessGroup(group_name=orcid, group_type=AccessGroupType.IDENTITY.value)
        session.add(ag)
        user = _user(session, orcid)
        session.commit()
        session.refresh(ag)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id, role="owner"))
        session.commit()

        assert is_project_owner(orcid, orcid, session) is False


def test_no_standing_at_all_is_not_ownership():
    engine = _engine()
    with Session(engine) as session:
        proj = _project(session, "proj_c", "0000-0005")
        session.add(AccessGroup(
            group_name=proj.unique_id,
            group_type=AccessGroupType.PROJECT.value,
        ))
        session.commit()

        assert is_project_owner("0000-0005", "proj_c", session) is False


def test_led_accessgroup_ids_aggregates_every_project_owned():
    from app.api.routes.access_groups import _led_accessgroup_ids

    engine = _engine()
    with Session(engine) as session:
        proj1 = _project(session, "proj_lead_1", "0000-0006")
        proj2 = _project(session, "proj_lead_2", "0000-0006")
        _project(session, "proj_not_led", "0000-0007")
        ag1 = AccessGroup(group_name=proj1.unique_id, group_type=AccessGroupType.PROJECT.value)
        ag2 = AccessGroup(group_name=proj2.unique_id, group_type=AccessGroupType.PROJECT.value)
        session.add(ag1)
        session.add(ag2)
        user = _user(session, "0000-0006")
        session.commit()
        session.refresh(ag1)
        session.refresh(ag2)

        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag1.id, role="owner"))
        session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=ag2.id, role="admin"))
        session.commit()

        assert set(_led_accessgroup_ids("0000-0006", session)) == {ag1.id}
