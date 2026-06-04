import re

from sqlmodel import select
from app.models.core_models import AccessGroup, User
from app.utils.sql_utils import add_or_update

_USERNAME_RE = re.compile(r'^[a-z][a-z0-9_-]{2,31}$')


def validate_username_format(username: str) -> str:
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "username must be 3-32 chars, start with a letter, "
            "and contain only lowercase letters, digits, underscores, hyphens"
        )
    return username


def sanitize_username_from_email(email: str) -> str:
    prefix = email.split('@')[0].lower()
    sanitized = re.sub(r'[^a-z0-9_-]', '_', prefix)
    sanitized = re.sub(r'^[^a-z]+', '', sanitized)
    return sanitized[:32]


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

    return ag
