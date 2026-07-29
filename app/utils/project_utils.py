from sqlalchemy import and_, join
from sqlmodel import select
from app.models.core_models import Project, User, UserProjectLink
from app.utils.sql_utils import add_or_update, touch_resource_modification_time
from app.utils.user_utils import sql_add_user_to_access_group


def sql_add_user_to_project(user: User, proj_id: str, session):
    """Add a user to a project's membership and access group. Idempotent."""
    add_or_update(session,
                  UserProjectLink,
                  where=(and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)),
                  user_id=user.id,
                  project_id=proj_id)
    sql_add_user_to_access_group(user.unique_id, proj_id, session)
    touch_resource_modification_time(session, proj_id)


def sql_get_projects_for_user(orcid, session, options=None):
    """Get all projects for a user by ORCID. Returns empty list if user not found."""
    user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    if user is None:
        return None

    query = (
        select(Project)
        .select_from(join(Project, UserProjectLink, Project.project_id == UserProjectLink.project_id))
        .where(UserProjectLink.user_id == user.id)
    )
    if options:
        query = query.options(*options)
    return session.exec(query).all()
