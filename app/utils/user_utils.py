from sqlmodel import select
from app.models.core_models import AccessGroup, User
from app.utils.sql_utils import add_or_update


def sql_get_user_by_email(email: str, session):
    return session.exec(select(User).where(User.email == email)).one_or_none()


def sql_add_user_to_access_group(orcid, group_name, session):
    """Add a user to an access group by ORCID. Creates the access group if it doesn't exist."""
    user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    ag = add_or_update(session,
                       AccessGroup,
                       AccessGroup.group_name == group_name,
                       group_name=group_name)

    if user and not (user in ag.users):
        ag.users.append(user)

    session.commit()
    session.refresh(ag)
    return ag
