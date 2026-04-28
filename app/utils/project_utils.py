from sqlalchemy import join
from sqlmodel import select
from app.models.core_models import Project, User, UserProjectLink


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
