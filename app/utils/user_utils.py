import logging
import re
from typing import Dict, List, Optional

import requests
from fastapi import HTTPException
from sqlalchemy import func
from sqlmodel import select
from app.general_utils import get_secret
from app.models.core_models import AccessGroup, Project, User
from app.utils.sql_utils import add_or_update

logger = logging.getLogger(__name__)

_USERNAME_RE = re.compile(r'^[a-z][a-z0-9_-]{2,31}$')

MF_PROPOSAL_DB_URL = "https://foundry-admin.lbl.gov/api/JSON/sciCat-GetUser.aspx"
MF_PROPOSAL_DB_TIMEOUT = 10


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


def find_mf_user_proposals(orcid: str) -> List[str]:
    """Return the MF proposal IDs associated with an ORCID.

    Returns [] on any failure so callers on the login and account-creation paths are
    not blocked by a proposal-DB outage. Failures are logged as warnings.
    """
    try:
        apikey = get_secret("PROPDB_APIKEY", "propdb-apikey/versions/1")
    except Exception as e:
        logger.warning("Could not load PROPDB_APIKEY, skipping proposal lookup: %s", e)
        return []
    if not apikey:
        logger.warning("PROPDB_APIKEY is unset, skipping proposal lookup for %s", orcid)
        return []

    try:
        response = requests.get(
            MF_PROPOSAL_DB_URL,
            params={"key": apikey, "orcid": orcid},
            timeout=MF_PROPOSAL_DB_TIMEOUT,
        )
        response.raise_for_status()
        users = response.json()
    except (requests.RequestException, ValueError) as e:
        logger.warning("MF proposal DB lookup failed for %s: %s", orcid, e)
        return []

    found_proposals = []
    for u in users:
        found_proposals += u.get('proposals', [])
    return sorted(set(found_proposals))


def assign_projects_to_user(session, orcid: str, project_ids: List[str]) -> Dict[str, List[str]]:
    """Additively link a user to projects and to each project's access group.

    Never removes an existing link — a proposal-DB outage or a manually granted project
    must not be able to revoke access. Does not commit; the caller owns the transaction.
    """
    user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    added, already_member, not_found = [], [], []

    for pname in sorted(set(pid for pid in project_ids if pid)):
        project = session.exec(select(Project).where(Project.project_id == pname)).one_or_none()
        if project is None:
            logger.warning("No project found for %s — skipping.", pname)
            not_found.append(pname)
            continue

        if project in user.projects:
            already_member.append(pname)
        else:
            user.projects.append(project)
            added.append(pname)

        sql_add_user_to_access_group(orcid, pname, session)

    session.add(user)
    return {
        "projects_added": added,
        "projects_already_member": already_member,
        "projects_not_found": not_found,
    }
