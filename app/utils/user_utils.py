import logging
import re
from typing import Optional, Sequence

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from anyascii import anyascii
from sqlmodel import select
from app.auth.identity import default_orcid_issuer, ensure_identity, find_identity
from app.general_utils import is_orcid, is_valid_mfid
from app.models.core_models import AccessGroup, User, UserAccessGroupLink
from app.utils.permissions import AccessGroupType, PlatformRole, Role

logger = logging.getLogger(__name__)
# urllib3 logs request URLs at DEBUG, and the proposal service takes its API key in the query string.
logging.getLogger("urllib3.connectionpool").setLevel(logging.WARNING)


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
    sanitized = anyascii(raw).replace("'", "").lower()
    sanitized = re.sub(r'[^a-z0-9_-]', '_', sanitized)
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


RESTRICTED_USERNAMES = frozenset({
    "admin", "administrator", "root", "system", "support", "security", "staff", "superuser",
    "crucible", "hub", "api", "me", "help", "account", "login", "logout",
    "null", "undefined", "anonymous",
    "search", "resolve", "by-username",
})
_RESTRICTED_KEYS = frozenset(_username_collision_key(name) for name in RESTRICTED_USERNAMES)


def is_restricted_username(username: str) -> bool:
    """Reserved names, and their lookalikes under the same case- and separator-insensitive rule."""
    return _username_collision_key(username) in _RESTRICTED_KEYS


def assert_self_chosen_username_allowed(username: Optional[str], current: Optional[str]) -> None:
    """For a username someone picks for themselves. Administrators can set a reserved one, and
    keeping the one you already have is always allowed."""
    if username is None or (current is not None and _username_collision_key(username) == _username_collision_key(current)):
        return
    if is_restricted_username(username):
        raise HTTPException(status_code=409, detail={"code": "username_restricted", "fields": ["username"], "message": "This username is reserved; choose another"})


def assert_username_available(session, username: str, exclude_orcid: Optional[str] = None) -> None:
    """Raise 409 if username is already taken by a different user — case-
    and separator-insensitive, so johndoe/john_doe/john-doe collide as one
    username. One query: the DB does the normalization, not a full-table
    scan pulled into Python."""
    collision_expr = func.lower(func.replace(func.replace(User.username, '_', ''), '-', ''))
    q = select(User).where(collision_expr == _username_collision_key(username))
    if exclude_orcid:
        q = q.where(User.unique_id != exclude_orcid)
    if session.exec(q).first() is not None:
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


def assert_username_kept_for_password(session, user, update_data: dict) -> None:
    """A password signs in with the username, so an account with one cannot clear it."""
    from app.models.core_models import LocalCredential

    if "username" in update_data and not update_data["username"] and session.get(LocalCredential, user.id) is not None:
        raise HTTPException(status_code=409, detail={"code": "username_required", "message": "This account signs in with a password, so it needs a username"})


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


def profile_conflict(error: IntegrityError) -> Optional[HTTPException]:
    """The 409 for a username or email that a concurrent request took first, or None for any other constraint."""
    message = str(error.orig)
    if "ux_user_email_lower" in message or "uq_user_email" in message:
        return fields_in_use(["email"])
    if "ix_user_username" in message or "user.username" in message:
        return fields_in_use(["username"])
    return None


def commit_user(session) -> None:
    """Commit, turning a concurrent username or email uniqueness conflict into the same 409."""
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        conflict = profile_conflict(error)
        if conflict is not None:
            raise conflict from None
        raise


def provision_user(session, *, unique_id: str, first_name: str, last_name: str, email: Optional[str], username: Optional[str],
                   project_groups: Sequence[AccessGroup] = (), platform_role: Optional[PlatformRole] = None, orcid_issuer: Optional[str] = None) -> User:
    """Add a person, always with a username, with their identity group (as owner) and contributor
    membership of the given project groups. The caller checks availability and commits. Every path
    that creates a person comes through here."""
    username = username or generate_unique_username(session, first_name, last_name)
    user = User(unique_id=unique_id, first_name=first_name, last_name=last_name, email=email, username=username, is_service_account=False)
    if platform_role is not None:
        user.platform_role = platform_role.value
    session.add(user)
    identity_group = session.exec(select(AccessGroup).where(AccessGroup.group_name == unique_id, AccessGroup.group_type == AccessGroupType.IDENTITY.value)).one_or_none()
    if identity_group is None:
        identity_group = AccessGroup(group_name=unique_id, group_type=AccessGroupType.IDENTITY.value)
        session.add(identity_group)
    session.flush()
    session.add(UserAccessGroupLink(user_id=user.id, accessgroup_id=identity_group.id, role=Role.OWNER.db_value))
    session.add_all([
        UserAccessGroupLink(user_id=user.id, accessgroup_id=group.id, role=Role.CONTRIBUTOR.db_value)
        for group in project_groups
    ])
    if is_orcid(unique_id):
        ensure_identity(session, user, orcid_issuer or default_orcid_issuer(), unique_id)
    return user


def identity_group_of(session, user_id: int) -> Optional[AccessGroup]:
    """A person's own access group, found through their owner membership rather than its name,
    so renaming the group never loses it."""
    from app.models.core_models import UserAccessGroupLink

    return session.exec(
        select(AccessGroup)
        .join(UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id)
        .where(UserAccessGroupLink.user_id == user_id, UserAccessGroupLink.role == Role.OWNER.db_value, AccessGroup.group_type == AccessGroupType.IDENTITY.value)
    ).first()


def identity_group_owner(session, group_id: int) -> Optional[User]:
    from app.models.core_models import UserAccessGroupLink

    return session.exec(
        select(User)
        .join(UserAccessGroupLink, UserAccessGroupLink.user_id == User.id)
        .where(UserAccessGroupLink.accessgroup_id == group_id, UserAccessGroupLink.role == Role.OWNER.db_value)
    ).first()


def is_username_available(session, username: str) -> bool:
    """Boolean form of assert_username_available, for generate-and-retry
    loops rather than request validation."""
    collision_expr = func.lower(func.replace(func.replace(User.username, '_', ''), '-', ''))
    q = select(User).where(collision_expr == _username_collision_key(username))
    return session.exec(q).first() is None


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
    while is_restricted_username(candidate) or not is_username_available(session, candidate):
        suffix = f"_{n}"
        candidate = f"{base[:_USERNAME_MAX - len(suffix)]}{suffix}"
        n += 1
    return candidate


def _visible(user: Optional[User], include_pending: bool) -> Optional[User]:
    """Accounts waiting for approval exist only for administrators reviewing them."""
    return user if user is not None and (include_pending or user.pending_at is None) else None


def user_by_id_or_orcid(session, value: str, *, include_pending: bool = False) -> Optional[User]:
    """The person whose unique_id this is, or whose linked ORCID it is, so an ORCID keeps working
    once it is no longer anyone's unique_id. Only the configured ORCID issuer counts, never a
    look-alike subject from another provider."""
    user = session.exec(select(User).where(User.unique_id == value)).one_or_none()
    if user is not None or not is_orcid(value):
        return _visible(user, include_pending)
    identity = find_identity(session, default_orcid_issuer(), value)
    return _visible(session.get(User, identity.user_id) if identity is not None else None, include_pending)


def person_by_reference(session, value: str, *, include_pending: bool = False) -> Optional[User]:
    """For exact filters: the person a unique_id, linked ORCID, username or email names, or None."""
    user = user_by_id_or_orcid(session, value, include_pending=include_pending)
    if user is not None:
        return user
    if "@" in value:
        return _visible(session.exec(select(User).where(func.lower(User.email) == value.lower())).one_or_none(), include_pending)
    return _visible(session.exec(select(User).where(User.username == value.lower())).one_or_none(), include_pending)


def owners_of(session, records) -> dict:
    """Owner rows by `user.id` for records that carry `owner_user_id`, in one query."""
    records = list(records)
    ids = {record.owner_user_id for record in records if record.owner_user_id is not None}
    return {user.id: user for user in session.exec(select(User).where(User.id.in_(ids))).all()} if ids else {}


def owner_fields(record, owners: dict, include_owner: bool = False) -> dict:
    """`owner_orcid`, and with include_owner the public `owner`, read from the owner's user row."""
    from app.models.users import UserPublicRead

    owner = owners.get(record.owner_user_id) if record.owner_user_id is not None else None
    fields = {"owner_orcid": owner.unique_id if owner is not None else None}
    if include_owner:
        fields["owner"] = UserPublicRead.model_validate(owner) if owner is not None else None
    return fields


def owner_unique_id(session, record) -> Optional[str]:
    owner = session.get(User, record.owner_user_id) if record.owner_user_id is not None else None
    return owner.unique_id if owner is not None else None


def owned_by(session, owner_user_id_column, value: str):
    """SQL test that the owner named by `value` holds the record; matches nothing for an unknown person."""
    from sqlalchemy import false

    owner = person_by_reference(session, value)
    return owner_user_id_column == owner.id if owner is not None else false()


def resolve_user(session, orcid: Optional[str] = None, email: Optional[str] = None, username: Optional[str] = None) -> User:
    """Resolve a user by ID or ORCID, email, or username (priority: orcid > email > username).
    Email matching is case-insensitive. Raises 400 if no identifier provided, 404 if not found.
    """
    if orcid:
        user = user_by_id_or_orcid(session, orcid)
    elif email:
        user = _visible(session.exec(select(User).where(func.lower(User.email) == email.lower())).one_or_none(), False)
    elif username:
        user = _visible(session.exec(select(User).where(User.username == username.lower())).one_or_none(), False)
    else:
        raise HTTPException(status_code=400, detail="One of orcid, email, or username must be provided")
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


def resolve_user_by_identifier(session, identifier: str) -> User:
    """Resolve a user from a single string that may be a unique_id, a linked ORCID, a
    username, or an email — distinguished by format rather
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


def sql_add_user_to_access_group(orcid, group_name, session, role=None, group_type=None, actor=None):
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
        close_pending_join_requests(session, ag.id, user, link.role if link is not None else (role or Role.CONTRIBUTOR).db_value, actor)
    return ag


def close_pending_join_requests(session, accessgroup_id, user, granted_role, actor=None):
    from sqlalchemy import update
    from app.general_utils import get_utc_datetime
    from app.models.core_models import AccessGroupJoinRequest

    session.execute(
        update(AccessGroupJoinRequest)
        .where(
            AccessGroupJoinRequest.accessgroup_id == accessgroup_id,
            AccessGroupJoinRequest.requester_user_id == user.id,
            AccessGroupJoinRequest.status == "pending",
        )
        .values(
            status="approved",
            review_time=get_utc_datetime(),
            reviewer_id=actor.unique_id if actor is not None else None,
            reviewer_user_id=actor.id if actor is not None else None,
            reviewer_notes="Added to the project directly",
            granted_role=granted_role,
        )
        .execution_options(synchronize_session=False)
    )
