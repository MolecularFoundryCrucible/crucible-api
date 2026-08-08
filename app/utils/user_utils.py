import re
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select
from app.models.core_models import AccessGroup, User
from app.utils.sql_utils import add_or_update

_USERNAME_RE = re.compile(r'^[a-z][a-z0-9_-]{2,31}$')


def validate_username_format(username: str) -> str:
    username = username.lower()
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


def assert_username_available(session, username: str, exclude_orcid: Optional[str] = None) -> None:
    """Raise 409 if username is already taken by a different user."""
    q = select(User).where(User.username == username.lower())
    if exclude_orcid:
        q = q.where(User.unique_id != exclude_orcid)
    if session.exec(q).one_or_none() is not None:
        raise HTTPException(status_code=409, detail="Username already taken")


def resolve_user(session, orcid: Optional[str] = None, email: Optional[str] = None, username: Optional[str] = None) -> User:
    """Resolve a user by ORCID, email, or username (priority: orcid > email > username).
    Email matching is case-insensitive. Raises 400 if no identifier provided, 404 if not found.
    """
    if orcid:
        user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    elif email:
        user = session.exec(select(User).where(func.lower(User.email) == email.lower())).one_or_none()
    elif username:
        user = session.exec(select(User).where(User.username == username.lower())).one_or_none()
    else:
        raise HTTPException(status_code=400, detail="One of orcid, email, or username must be provided")
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def sql_add_user_to_access_group(orcid, group_name, session, role=None):
    """Add a user to an access group by ORCID, creating the group if needed.

    `role` is the standing in that group, defaulting to contributor. Appending
    via the relationship would leave it null, which fails closed to viewer —
    so a newly added project member would land read-only.
    """
    from app.models.core_models import UserAccessGroupLink
    from app.utils.permissions import Role

    user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    ag = add_or_update(session,
                       AccessGroup,
                       AccessGroup.group_name == group_name,
                       group_name=group_name)
    if user is None:
        return ag

    session.flush()  # ag.id
    link = session.exec(
        select(UserAccessGroupLink)
        .where(UserAccessGroupLink.user_id == user.id)
        .where(UserAccessGroupLink.accessgroup_id == ag.id)
    ).one_or_none()
    if link is None:
        session.add(UserAccessGroupLink(
            user_id=user.id, accessgroup_id=ag.id,
            role=(role or Role.CONTRIBUTOR).db_value))
    elif link.role is None:
        link.role = (role or Role.CONTRIBUTOR).db_value
        session.add(link)
    return ag
