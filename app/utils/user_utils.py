import logging
import re
from typing import Dict, List, Optional

import requests
from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from app.general_utils import get_secret, is_orcid, is_valid_mfid
from app.models.core_models import AccessGroup, Project, User

logger = logging.getLogger(__name__)

MF_PROPOSAL_DB_URL = "https://foundry-admin.lbl.gov/api/JSON/sciCat-GetUser.aspx"
MF_PROPOSAL_DB_TIMEOUT = 10

# A separator (- or _) must always be followed by an alphanumeric run, which
# structurally rules out leading, trailing, and consecutive separators in one
# pattern. 24-char cap (not 32) is deliberate: it makes a 26-char MFID
# (service account identity) provably unable to collide with a username by
# format, rather than relying on lookup-order precedence to keep them apart.
_USERNAME_RE = re.compile(r'^[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*$')
_USERNAME_MAX = 24


def validate_username_format(username: str) -> str:
    username = username.lower()
    if not (3 <= len(username) <= _USERNAME_MAX) or not _USERNAME_RE.fullmatch(username):
        raise ValueError(
            f"username must be 3-{_USERNAME_MAX} chars, start with a letter, contain only "
            "lowercase letters, digits, underscores, hyphens, and never have "
            "a leading, trailing, or repeated separator"
        )
    return username


def _sanitize_username_candidate(raw: str) -> str:
    """Shared cleanup: satisfies validate_username_format's rules by
    construction — callers that skip re-validating (the backfill script
    does) rely on that. May still come out empty or under 3 chars; the
    caller checks length before use."""
    sanitized = re.sub(r'[^a-z0-9_-]', '_', raw.lower())
    sanitized = re.sub(r'[_-]{2,}', '_', sanitized)
    sanitized = re.sub(r'^[^a-z]+', '', sanitized)
    sanitized = sanitized[:_USERNAME_MAX]
    return sanitized.rstrip('_-')


def sanitize_username_from_email(email: str) -> str:
    """Derive a candidate username from an email's local part."""
    return _sanitize_username_candidate(email.split('@')[0])


def sanitize_username_from_name(first_name: str, last_name: str) -> str:
    """Derive a candidate username from first/last name — used at login,
    where email isn't reliably available yet (ORCID doesn't always return
    one), but first/last name always are."""
    return _sanitize_username_candidate(f"{first_name}.{last_name}")


def _username_collision_key(username: str) -> str:
    """Separator-insensitive normalization: johndoe, john_doe, and john-doe
    are the same username for collision purposes — distinct only by
    separator placement is exactly the kind of lookalike confusion usernames
    should not allow."""
    return re.sub(r'[_-]', '', username.lower())


def assert_username_available(session, username: str, exclude_orcid: Optional[str] = None) -> None:
    """Raise 409 if username is already taken by a different user — case-
    and separator-insensitive, so johndoe/john_doe/john-doe collide as one
    username. One query: the DB does the normalization, not a full-table
    scan pulled into Python."""
    collision_expr = func.lower(func.replace(func.replace(User.username, '_', ''), '-', ''))
    q = select(User).where(collision_expr == _username_collision_key(username))
    if exclude_orcid:
        q = q.where(User.unique_id != exclude_orcid)
    if session.exec(q).one_or_none() is not None:
        raise fields_in_use(["username"])


def fields_in_use(fields: list[str]) -> HTTPException:
    message = {("username",): "Username already taken", ("email",): "Email is already in use"}.get(tuple(fields), "Username and email are already in use")
    return HTTPException(status_code=409, detail={"code": "already_in_use", "fields": fields, "message": message})


def email_in_use(session, email: Optional[str], exclude_orcid: Optional[str] = None) -> bool:
    if email is None:
        return False
    q = select(User.id).where(func.lower(User.email) == email.lower())
    if exclude_orcid:
        q = q.where(User.unique_id != exclude_orcid)
    return session.exec(q).first() is not None


def assert_profile_fields_available(session, *, username: Optional[str] = None, email: Optional[str] = None, exclude_orcid: Optional[str] = None) -> None:
    """Raise one 409 listing every field (username, email) already held by another user."""
    conflicts = []
    if username is not None:
        try:
            assert_username_available(session, username, exclude_orcid=exclude_orcid)
        except HTTPException:
            conflicts.append("username")
    if email_in_use(session, email, exclude_orcid):
        conflicts.append("email")
    if conflicts:
        raise fields_in_use(conflicts)


def commit_user(session) -> None:
    """Commit, turning a concurrent email uniqueness conflict into the same 409."""
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        if "ux_user_email_lower" in str(error.orig):
            raise fields_in_use(["email"]) from None
        raise


def is_username_available(session, username: str) -> bool:
    """Boolean form of assert_username_available, for generate-and-retry
    loops rather than request validation."""
    collision_expr = func.lower(func.replace(func.replace(User.username, '_', ''), '-', ''))
    q = select(User).where(collision_expr == _username_collision_key(username))
    return session.exec(q).one_or_none() is None


def generate_unique_username(session, first_name: str, last_name: str) -> str:
    """A memorable username for a user who doesn't have one yet (e.g. first
    login, before username was required) — derived from their name, with a
    numeric suffix (_2, _3, ...) on collision. Same deconflict shape as
    migrations/data/20260603_backfill_username_from_email.py, generalized to
    the separator-insensitive collision check.

    Falls back to a generic prefix if name sanitizes to nothing usable
    (e.g. a name with no ASCII letters) — this must always return something
    valid, since it exists specifically to make username non-optional.
    """
    base = sanitize_username_from_name(first_name, last_name)
    if len(base) < 3:
        base = "user"

    candidate = base
    n = 2
    while not is_username_available(session, candidate):
        suffix = f"_{n}"
        candidate = f"{base[:_USERNAME_MAX - len(suffix)]}{suffix}"
        n += 1
    return candidate


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


def resolve_user_by_identifier(session, identifier: str) -> User:
    """Resolve a user from a single string that may be an ORCID, a service
    account's MFID, a username, or an email — distinguished by format rather
    than requiring the caller to name which field they mean.

    The four never collide: email always contains '@'; ORCID is a strict
    case-sensitive NNNN-NNNN-NNNN-NNNN pattern; a service account's identity
    (stored in the same column as an ORCID) is a 26-char MFID; and usernames
    are capped at 24 chars and enforced lowercase, so an MFID can never
    validate as one. Checking identity-shape before falling back to username
    is what keeps that last pair apart in practice.
    """
    if "@" in identifier:
        return resolve_user(session, email=identifier)
    if is_orcid(identifier) or is_valid_mfid(identifier):
        return resolve_user(session, orcid=identifier)
    return resolve_user(session, username=identifier)


def resolve_service_account_reference(session, ref: str) -> User:
    if is_valid_mfid(ref):
        query = select(User).where(User.unique_id == ref)
    else:
        query = select(User).where(User.username == ref.lower())
    user = session.exec(query.where(User.is_service_account == True)).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="Service account not found")
    return user


def resolve_create_owner(session, owner: str | None, owner_orcid: str | None, authenticated_user_id: str) -> User:
    if owner is not None:
        return resolve_user_by_identifier(session, owner)
    return resolve_user(session, orcid=owner_orcid or authenticated_user_id)


def sql_add_user_to_access_group(orcid, group_name, session, role=None, group_type=None, actor_id=None):
    """Add a user to an access group by ORCID, creating the group if needed.

    `role` is the standing in that group, defaulting to contributor. Appending
    via the relationship would leave it null, which fails closed to viewer,
    so a newly added project member would land read-only.
    """
    from app.models.core_models import UserAccessGroupLink
    from app.utils.permissions import AccessGroupType, Role

    user = session.exec(select(User).where(User.unique_id == orcid)).one_or_none()
    ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
    expected_type = group_type.value if hasattr(group_type, "value") else group_type
    if ag is None:
        ag = AccessGroup(
            group_name=group_name,
            group_type=expected_type or AccessGroupType.CUSTOM.value,
        )
        session.add(ag)
    elif expected_type is not None and ag.group_type != expected_type:
        raise HTTPException(
            status_code=409,
            detail=f"Access group '{group_name}' is {ag.group_type}, expected {expected_type}",
        )
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
    if ag.group_type == AccessGroupType.PROJECT.value:
        close_pending_join_requests(session, ag.id, orcid, link.role if link is not None else (role or Role.CONTRIBUTOR).db_value, actor_id)
    return ag


def close_pending_join_requests(session, accessgroup_id, orcid, granted_role, actor_id=None):
    from sqlalchemy import update
    from app.general_utils import get_utc_datetime
    from app.models.core_models import AccessGroupJoinRequest

    session.execute(
        update(AccessGroupJoinRequest)
        .where(
            AccessGroupJoinRequest.accessgroup_id == accessgroup_id,
            AccessGroupJoinRequest.requester_id == orcid,
            AccessGroupJoinRequest.status == "pending",
        )
        .values(
            status="approved",
            review_time=get_utc_datetime(),
            reviewer_id=actor_id,
            reviewer_notes="Added to the project directly",
            granted_role=granted_role,
        )
        .execution_options(synchronize_session=False)
    )


def find_mf_user_proposals(orcid: str) -> List[str]:
    """Return MF proposal IDs for an ORCID without blocking on service failures."""
    try:
        api_key = get_secret("PROPDB_APIKEY", "propdb-apikey/versions/1")
    except Exception as exc:
        logger.warning("Could not load proposal service credentials: %s", exc)
        return []
    if not api_key:
        logger.warning("Proposal service credentials are unavailable")
        return []

    try:
        response = requests.get(
            MF_PROPOSAL_DB_URL,
            params={"key": api_key, "orcid": orcid},
            timeout=MF_PROPOSAL_DB_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("MF proposal lookup failed for %s: %s", orcid, exc)
        return []

    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        logger.warning("MF proposal lookup returned an invalid response for %s", orcid)
        return []
    if any(not isinstance(item.get("proposals", []), list) for item in payload):
        logger.warning("MF proposal lookup returned invalid proposals for %s", orcid)
        return []
    return sorted({
        proposal
        for item in payload
        for proposal in item.get("proposals", [])
        if isinstance(proposal, str)
    })


def assign_projects_to_user(session, user_id: str, project_ids: List[str]) -> Dict[str, List[str]]:
    """Add project memberships without removing existing memberships."""
    from app.utils.permissions import AccessGroupType, Role

    user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    added = []
    already_member = []
    not_found = []
    for project_id in sorted(set(project_id for project_id in project_ids if project_id)):
        project = session.exec(select(Project).where(func.lower(Project.project_id) == project_id.lower())).one_or_none()
        if project is None:
            logger.warning("No project found for %s", project_id)
            not_found.append(project_id)
            continue

        if project in user.projects:
            already_member.append(project_id)
        else:
            user.projects.append(project)
            added.append(project_id)
        sql_add_user_to_access_group(
            user_id,
            project.unique_id,
            session,
            role=Role.CONTRIBUTOR,
            group_type=AccessGroupType.PROJECT,
        )

    session.add(user)
    return {
        "projects_added": added,
        "projects_already_member": already_member,
        "projects_not_found": not_found,
    }
