from typing import Annotated, Callable, Iterable, Optional, Sequence
import logging
import os
import hashlib
import threading
import secrets
import datetime
from dataclasses import replace
from cachetools import TTLCache

logger = logging.getLogger(__name__)

from fastapi import Depends, Header, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import get_secret
from app.api.dependencies import get_engine
from app.tables import ApiKey, ApiToken, User
from app.models.resource import ResourceCapabilities
from app.utils.hub_tokens import ACCESS_PREFIX, DEVICE_CODE, InvalidHubGrant, hub_access_family
from app.auth.api_keys import DEFAULT_KEY_NAME, find_api_key, replace_plain_keys, status_of, store_key
from app.services.activity import record_caller_activity
from app.utils.permissions import (
    PUBLIC_GROUP_NAME, SESSION, THRESHOLDS, GroupMembership, Permission, PlatformRole,
    Principal, PrivilegeMode, PrivilegeModeSource, Role, can_grant,
    effective_role, has_global_permission, has_permission,
    resolve_authorization_context)
from sqlmodel import Session, select
from sqlalchemy.sql import and_, or_
from datetime import date, timedelta

# ====== Constants
security = HTTPBearer()
PRIVILEGE_MODE_HEADER = "Crucible-Privilege-Mode"

# ====== Auth cache
# Positive-only cache of validated tokens -> Principal.
# Per-worker and per-pod: a revoked/rotated key stays valid for up to the TTL.
_AUTH_CACHE_TTL = int(os.environ.get("AUTH_CACHE_TTL", "60"))
_AUTH_CACHE_MAXSIZE = int(os.environ.get("AUTH_CACHE_MAXSIZE", "10000"))
_auth_cache = TTLCache(maxsize=_AUTH_CACHE_MAXSIZE, ttl=_AUTH_CACHE_TTL)
_auth_cache_lock = threading.Lock()


# Resolved once per process: the public group row is created by the phase 1
# migration and never changes.
_UNSET = object()
_PUBLIC_GROUP_ID = _UNSET


def _standing(value) -> Role:
    """Group standing from the database, failing closed to viewer on null or
    garbage. Null means the backfill has not reached this row."""
    if value is None:
        return Role.VIEWER
    try:
        return Role.from_db(value)
    except KeyError:
        logger.warning("Unknown role %r on useraccessgrouplink; treating as viewer", value)
        return Role.VIEWER


def _platform_role(value) -> PlatformRole:
    if value is None:
        return PlatformRole.NONE
    try:
        return PlatformRole(value)
    except ValueError:
        logger.warning("Unknown platform_role %r; treating as none", value)
        return PlatformRole.NONE


def _auth_cache_key(apikey: str) -> str:
    return hashlib.sha256(apikey.encode()).hexdigest()


def forget_cached_key_hash(digest: str) -> None:
    """The cache is keyed by the key's SHA-256, so a key known only by its hash can be dropped."""
    with _auth_cache_lock:
        _auth_cache.pop(digest, None)


def forget_cached_keys(session, user_id: int) -> None:
    """Drop this person's keys from this worker's auth cache. Other workers follow within the
    cache lifetime."""
    for digest in session.exec(select(ApiKey.key_hash).where(ApiKey.user_id == user_id)).all():
        forget_cached_key_hash(digest)


# ====== Functions
def find_or_add_api_access(user_id: int, engine, regenerate = False):

    one_year_ago = (date.today()-timedelta(365)).isoformat()
    with Session(engine) as session:
        # The row lock is what keeps simultaneous requests from each adding a key.
        user = session.exec(select(User).where(User.id == user_id).with_for_update()).one()
        api_acct = session.exec(select(ApiToken).where(and_(ApiToken.user_id == user_id,
                                                            ApiToken.token_created > one_year_ago))).one_or_none()

        if api_acct is not None and regenerate == False:
            return(api_acct.token)

        elif api_acct is not None and regenerate == True:
            replaced = replace_plain_keys(session, user_id)
            new_token = generate_apikey(user, session)
            for digest in replaced:
                forget_cached_key_hash(digest)
            return(new_token)

        else:
            new_token = generate_apikey(user, session)
            return(new_token)


def add_apikey(user, session) -> str:
    """Add a new API key for this User row to the session without committing."""
    new_token = secrets.token_hex(32)
    session.add(ApiToken(user_id=user.id, user_unique_id=user.unique_id, access_group_id=user.unique_id, token=new_token, token_created=date.today().isoformat()))
    store_key(session, user.id, new_token, name=DEFAULT_KEY_NAME)
    return new_token


def generate_apikey(user, session):
    new_token = add_apikey(user, session)
    session.commit()
    return(new_token)

    
def get_secret_key() -> str:
    return get_secret("CRUCIBLE_SECRET_KEY")


def find_key(credentials, engine):
    """The active ApiKey for the presented key, found by its hash. Raises 401 for a key that is
    unknown, expired or revoked, alike."""
    with Session(engine) as session:
        api_acct = find_api_key(session, credentials.credentials)
        if api_acct is None or status_of(api_acct) != "active":
            # Return an identical response whether the key never existed, expired or was revoked,
            # so the error can't be used to probe keys. Details go to the server log only.
            if api_acct is not None:
                logger.warning("%s API key presented (user=%s, key=%s)", status_of(api_acct).capitalize(), api_acct.user_id, api_acct.id)
            raise HTTPException(status_code=401, detail="Invalid or expired API key.")
        session.commit()
        session.refresh(api_acct)
        session.expunge(api_acct)
    return api_acct


def authenticate_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> Principal:
    """Resolve the caller to a Principal. Raises 401 if the key is invalid."""
    return _authorize_user(credentials)


def authorize_user(
    request: Request,
    principal: Annotated[Principal, Depends(authenticate_user)],
    privilege_mode: Annotated[
        Optional[PrivilegeMode],
        Header(
            alias=PRIVILEGE_MODE_HEADER,
            description=(
                "Select normal ACL-derived authorization or explicit "
                "platform-administrator elevation. Omission preserves legacy behavior."
            ),
        ),
    ] = None,
) -> Principal:
    request.state.authenticated_actor = principal
    request.state.requested_privilege_mode = privilege_mode
    try:
        context = resolve_authorization_context(principal, privilege_mode)
    except PermissionError:
        request.state.privilege_mode_source = PrivilegeModeSource.EXPLICIT
        raise HTTPException(
            status_code=403,
            detail="Caller is not eligible for elevated privilege mode",
        )
    request.state.authorization_context = context
    return context.principal


def _authorize_user(credentials: HTTPAuthorizationCredentials) -> Principal:
    engine = get_engine()
    apikey = credentials.credentials
    if apikey.startswith(ACCESS_PREFIX):
        with Session(engine) as session:
            try:
                family = hub_access_family(session, apikey)
            except InvalidHubGrant:
                raise HTTPException(status_code=401, detail="Invalid or expired API credential.")
            authenticated_at = family.auth_time.replace(tzinfo=family.auth_time.tzinfo or datetime.timezone.utc) if family.auth_time else None
            principal = replace(load_principal(session, family.user_id), credential=SESSION, authenticated_at=authenticated_at, session_family_id=family.id, client_id=family.client_id,
                                via_device=family.grant_type == DEVICE_CODE)
        record_caller_activity(engine, principal.user_id)
        return principal

    cache_key = _auth_cache_key(apikey)
    with _auth_cache_lock:
        cached = _auth_cache.get(cache_key)
    if cached is not None:
        record_caller_activity(engine, cached.user_id, apikey)
        return cached

    api_acct = find_key(credentials, engine)  # raises 401 on invalid/expired

    if api_acct is None:
        raise HTTPException(status_code=401, detail="Invalid or expired API key.")

    with Session(engine) as session:
        if session.exec(select(User.id).where(User.id == api_acct.user_id, or_(User.disabled_at != None, User.pending_at != None))).first() is not None:  # noqa: E711
            raise HTTPException(status_code=401, detail="Invalid or expired API key.")
        result = load_principal(session, api_acct.user_id)

    with _auth_cache_lock:
        _auth_cache[cache_key] = result
    record_caller_activity(engine, result.user_id, apikey)
    return result


def load_principal(session, user_id: Optional[int]) -> Principal:
    """Build a Principal from the database. Split out of authorize_user so the
    query is testable without an API key or a live request."""
    from app.tables import AccessGroup, User, UserAccessGroupLink

    user = session.get(User, user_id) if user_id is not None else None
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired API key.")

    # One query against the link rows rather than user.accessgroups, because the
    # standing lives on the link, not on the group.
    rows = session.exec(
        select(AccessGroup.id, AccessGroup.group_name, UserAccessGroupLink.role)
        .join(UserAccessGroupLink,
              UserAccessGroupLink.accessgroup_id == AccessGroup.id)
        .where(UserAccessGroupLink.user_id == user.id)
    ).all()

    groups = [
        GroupMembership(id=gid, group_name=name, role=_standing(role))
        for gid, name, role in rows
    ]

    # Inject the virtual public group. Membership is never stored — every
    # principal is in it, always at viewer, so min() caps any grant at viewer
    # and world-writable stays unrepresentable.
    #
    # This is what preserves the old `Resource.public == True` shortcut: the
    # pre-role check granted read to anyone on a public resource, and without
    # this injection everyone would lose that at cutover. Anything enumerating
    # "which groups am I in" must use Principal.real_group_names().
    public_id = _public_group_id(session)
    if public_id is not None and not any(g.id == public_id for g in groups):
        groups.append(GroupMembership(id=public_id, group_name=PUBLIC_GROUP_NAME,
                                      role=Role.VIEWER))

    # Materialize to plain, immutable data so the cached value is safe to share
    # across threads and doesn't depend on the closed session.
    return Principal(
        user_id=user.unique_id,
        internal_user_id=user.id,
        platform_role=_platform_role(user.platform_role),
        is_service_account=bool(user.is_service_account),
        groups=tuple(groups),
    )


def _public_group_id(session) -> Optional[int]:
    """Id of the virtual public group, cached for the process. None before the
    phase 1 migration, making injection a no-op rather than an error."""
    global _PUBLIC_GROUP_ID
    if _PUBLIC_GROUP_ID is _UNSET:
        from app.tables import AccessGroup
        _PUBLIC_GROUP_ID = session.exec(
            select(AccessGroup.id).where(AccessGroup.group_name == PUBLIC_GROUP_NAME)
        ).first()
    return _PUBLIC_GROUP_ID


# ====== Role-based checks

def fetch_acl_rows(session, mfid: str):
    """(accessgroup_id, permission) for one resource."""
    from app.tables import ResourceACL
    rows = session.exec(
        select(ResourceACL.accessgroup_id, ResourceACL.permission)
        .where(ResourceACL.mfid == mfid)
    ).all()
    return [(gid, Role.from_db(r)) for gid, r in rows if r is not None]


def _has_resource_permission(principal: Principal, acl_rows, permission: Permission) -> bool:
    if has_global_permission(principal, permission):
        return True
    return has_permission(principal, acl_rows, permission)


def resource_capabilities(session, principal: Principal, mfid: str, *,
                          manage_access_floor: Role = THRESHOLDS[Permission.MANAGE_ACL],
                          allow_peer_grants: bool = True,
                          status_permission: Optional[Permission] = Permission.DELETE,
                          acl_rows: Optional[Sequence[tuple[int, Role]]] = None,
                          can_delete: bool | Callable[[], bool] = False,
                          request_deletion_with: Optional[Permission] = None,
                          deletion_requested: Optional[bool] = None) -> ResourceCapabilities:
    if acl_rows is None:
        acl_rows = fetch_acl_rows(session, mfid)
    actor_role = effective_role(principal, acl_rows)

    max_grant_role = None
    if principal.is_platform_admin:
        max_grant_role = Role.ADMIN.db_value
    elif not principal.is_service_account:
        for target in (Role.ADMIN, Role.EDITOR, Role.CONTRIBUTOR, Role.VIEWER):
            if can_grant(
                actor_role,
                target,
                floor=manage_access_floor,
                allow_peer=allow_peer_grants,
            ):
                max_grant_role = target.db_value
                break

    may_delete = (
        _has_resource_permission(principal, acl_rows, Permission.DELETE)
        or _has_resource_permission(principal, acl_rows, Permission.HARD_DELETE)
    ) and (can_delete() if callable(can_delete) else can_delete)

    if request_deletion_with is None:
        may_request_deletion = may_delete
    else:
        may_request_deletion = _has_resource_permission(principal, acl_rows, request_deletion_with)
    if may_request_deletion:
        if deletion_requested is None:
            from app.utils.sql_utils import get_active_deletion_request
            deletion_requested = get_active_deletion_request(session, mfid) is not None
        may_request_deletion = not deletion_requested

    from app.services.resource_access import record_role

    return ResourceCapabilities(
        can_edit=_has_resource_permission(
            principal, acl_rows, Permission.EDIT),
        can_manage_access=max_grant_role is not None,
        can_view_access=principal.is_platform_admin or record_role(principal, acl_rows) is not None,
        can_change_status=(
            status_permission is not None
            and _has_resource_permission(
                principal, acl_rows, status_permission)
        ),
        can_transfer=_has_resource_permission(
            principal, acl_rows, Permission.TRANSFER),
        can_delete=may_delete,
        can_request_deletion=may_request_deletion,
        max_grant_role=max_grant_role,
    )


def require_permission(session, principal: Principal, mfid: str,
                       permission: Permission,
                       acl_rows: Optional[Sequence[tuple[int, Role]]] = None) -> Optional[Role]:
    """Raise 403 unless `principal` may perform `permission` on `mfid`.

    Returns the effective ACL role, so a caller needing to branch further does
    not recompute it. None means access came from a platform role rather than
    an ACL entry.

    """
    if acl_rows is None:
        acl_rows = fetch_acl_rows(session, mfid)
    if not _has_resource_permission(principal, acl_rows, permission):
        raise HTTPException(status_code=403, detail="Access denied")
    if principal.is_platform_admin:
        return None
    return effective_role(principal, acl_rows)


GRANTABLE_ROLES = (Role.VIEWER, Role.CONTRIBUTOR, Role.EDITOR, Role.ADMIN)


def entry_capabilities(principal: Principal, actor: Optional[Role], current: Optional[Role], *,
                       floor: Role, is_self: bool = False, fixed: bool = False) -> dict:
    """The capabilities object for one member or grant, decided by the same rule as require_grant."""

    if fixed or current is Role.OWNER:
        return {"can_remove": False, "can_change_role": False, "assignable_roles": []}
    if principal.is_platform_admin:
        assignable = list(GRANTABLE_ROLES)
        may_remove = True
    elif principal.is_service_account:
        assignable, may_remove = [], False
    else:
        assignable = [role for role in GRANTABLE_ROLES if can_grant(actor, role, current, floor=floor)]
        may_remove = can_grant(actor, current or Role.VIEWER, current, floor=floor)
    return {
        "can_remove": may_remove or is_self,
        "can_change_role": any(role != current for role in assignable),
        "assignable_roles": [role.db_value for role in assignable],
    }


def require_grant(session, principal: Principal, mfid: str, target: Role,
                  current: Optional[Role] = None, floor: Role = Role.ADMIN,
                  allow_peer: bool = True) -> None:
    """Raise 403 unless `principal` may set a grant on `mfid` to `target`.

    The cap binds on both ends. `floor` controls the minimum role that may
    manage grants, while `allow_peer` controls whether equal roles are within
    that authority.
    """

    if principal.is_platform_admin:
        return
    if principal.is_service_account:
        raise HTTPException(status_code=403, detail="Access denied")

    actor = effective_role(principal, fetch_acl_rows(session, mfid))
    if not can_grant(
        actor,
        target,
        current,
        floor=floor,
        allow_peer=allow_peer,
    ):
        raise HTTPException(
            status_code=403,
            detail=(f"You hold {actor.name.lower() if actor else 'no role'} here "
                    f"and cannot grant {target.name.lower()}"))


def permission_filter(principal: Principal, resource_model,
                      permission: Permission = Permission.READ,
                      resource_id_column=None):
    """SQL filter restricting a list query to resources the caller may reach.

    Relies on min(a,b) >= r being equivalent to a >= r and b >= r: the standing
    side is filtered in Python, so only a single column comparison reaches SQL
    and useraccessgrouplink never has to be joined.
    """
    from sqlalchemy import exists, false, true
    from app.tables import ResourceACL

    if has_global_permission(principal, permission):
        return true()
    if (
        principal.is_service_account
        and THRESHOLDS[permission] > Role.CONTRIBUTOR
    ):
        return false()

    threshold = THRESHOLDS[permission]
    group_ids = principal.group_ids_at_or_above(threshold)
    if not group_ids:
        return false()
    if resource_id_column is None:
        resource_id_column = resource_model.unique_id

    # Set membership, not `role >= threshold`. The column is declared str so
    # SQLite can create the table, and there `>=` compares alphabetically —
    # 'owner' >= 'viewer' is false — so a range comparison is right on Postgres
    # and silently wrong everywhere else. With at most five roles the IN list
    # is trivially indexable and depends on no enum ordering at all.
    allowed = [r.db_value for r in Role if r >= threshold]

    # correlate() pins the subquery to the outer model. Without it SQLAlchemy
    # auto-correlates ResourceACL too and the EXISTS ends up with no FROM
    # clause wherever the outer query already mentions that table.
    return (
        exists()
        .where(
            ResourceACL.mfid == resource_id_column,
            ResourceACL.accessgroup_id.in_(group_ids),
            ResourceACL.permission.in_(allowed),
        )
        .select_from(ResourceACL)
        .correlate(resource_model)
    )


def owner_affiliation_filter(principal: Principal, resource_model):
    from sqlalchemy import exists
    from app.tables import AccessGroup, ResourceACL
    from app.utils.permissions import AccessGroupType

    return (
        exists()
        .where(
            ResourceACL.mfid == resource_model.unique_id,
            ResourceACL.permission == Role.OWNER.db_value,
            ResourceACL.accessgroup_id == AccessGroup.id,
            AccessGroup.group_type == AccessGroupType.IDENTITY.value,
            AccessGroup.id.in_(_own_identity_group_ids(principal)),
        )
        .select_from(ResourceACL, AccessGroup)
        .correlate(resource_model)
    )


def member_affiliation_filter(principal: Principal, resource_model):
    """Resources the caller reaches through one of their own grants, not only because they are public."""
    from sqlalchemy import exists, false
    from app.tables import ResourceACL

    group_ids = [m.id for m in principal.groups if m.group_name != PUBLIC_GROUP_NAME and m.role >= Role.VIEWER]
    if not group_ids:
        return false()
    return (
        exists()
        .where(
            ResourceACL.mfid == resource_model.unique_id,
            ResourceACL.accessgroup_id.in_(group_ids),
            ResourceACL.permission.in_([r.db_value for r in Role if r >= Role.VIEWER]),
        )
        .select_from(ResourceACL)
        .correlate(resource_model)
    )


def resource_affiliation_filter(principal: Principal, resource_model, affiliation):
    from sqlalchemy import or_
    from app.models.query_params import ResourceAffiliation
    predicates = {
        ResourceAffiliation.OWNER: owner_affiliation_filter,
        ResourceAffiliation.MEMBER: member_affiliation_filter,
    }
    return or_(*(predicates[value](principal, resource_model) for value in dict.fromkeys(affiliation)))


def maintainer_affiliation_filter(principal: Principal, resource_model):
    """Resources where the caller's own identity holds editor or admin."""
    from sqlalchemy import exists
    from app.tables import AccessGroup, ResourceACL
    from app.utils.permissions import AccessGroupType

    return (
        exists()
        .where(
            ResourceACL.mfid == resource_model.unique_id,
            ResourceACL.permission.in_((Role.EDITOR.db_value, Role.ADMIN.db_value)),
            ResourceACL.accessgroup_id == AccessGroup.id,
            AccessGroup.group_type == AccessGroupType.IDENTITY.value,
            AccessGroup.id.in_(_own_identity_group_ids(principal)),
        )
        .select_from(ResourceACL, AccessGroup)
        .correlate(resource_model)
    )


def project_member_filter(user_id: str, project_model, roles: Optional[Iterable[Role]] = None):
    """Projects where `user_id` stands in the project's own access group,
    optionally at one of `roles`."""
    from sqlalchemy import exists
    from app.tables import AccessGroup, User, UserAccessGroupLink
    from app.utils.permissions import AccessGroupType

    conditions = [
        AccessGroup.group_name == project_model.unique_id,
        AccessGroup.group_type == AccessGroupType.PROJECT.value,
        UserAccessGroupLink.accessgroup_id == AccessGroup.id,
        UserAccessGroupLink.user_id == User.id,
        User.unique_id == user_id,
    ]
    if roles:
        conditions.append(UserAccessGroupLink.role.in_([Role.from_db(r).db_value for r in roles]))
    return (
        exists()
        .where(*conditions)
        .select_from(AccessGroup, UserAccessGroupLink, User)
        .correlate(project_model)
    )


def project_owner_affiliation_filter(principal: Principal, project_model):
    return project_member_filter(principal.user_id, project_model, roles=(Role.OWNER,))


def access_group_permission_filter(accessgroup_id: int, resource_model,
                                   permission: Permission = Permission.READ):
    from sqlalchemy import exists
    from app.tables import ResourceACL

    threshold = THRESHOLDS[permission]
    allowed = [role.db_value for role in Role if role >= threshold]
    return (
        exists()
        .where(
            ResourceACL.mfid == resource_model.unique_id,
            ResourceACL.accessgroup_id == accessgroup_id,
            ResourceACL.permission.in_(allowed),
        )
        .select_from(ResourceACL)
        .correlate(resource_model)
    )


def project_scope_filter(session, caller: Principal, resource_model,
                         project_mfid: str, project_scope: str):
    from app.tables import AccessGroup, Project
    from app.utils.permissions import AccessGroupType

    if project_scope == "assigned":
        return resource_model.project_mfid == project_mfid

    project = session.exec(
        select(Project).where(Project.unique_id == project_mfid)
    ).one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail=f"Project '{project_mfid}' not found")
    if (
        not has_global_permission(caller, Permission.READ)
        and caller.role_in(project.unique_id) is None
    ):
        raise HTTPException(status_code=403, detail="Not authorized to inspect project access")

    group = session.exec(
        select(AccessGroup).where(
            AccessGroup.group_name == project.unique_id,
            AccessGroup.group_type == AccessGroupType.PROJECT.value,
        )
    ).one_or_none()
    if group is None:
        raise HTTPException(
            status_code=409,
            detail=f"Project '{project.project_id}' has no project access group",
        )

    project_access = access_group_permission_filter(group.id, resource_model)
    if project_scope == "shared":
        return and_(
            project_access,
            resource_model.project_mfid.is_distinct_from(project_mfid),
        )
    return project_access


def require_platform_admin(principal: Principal) -> None:
    """Raise 403 unless the caller has platform-wide administrator authority."""
    if principal.is_platform_admin:
        return
    raise HTTPException(status_code=403, detail="Not authorized")


STEP_UP_MAX_AGE = 600
def require_recent_authentication(principal: Principal) -> None:
    """Raise the RFC 9470 step-up challenge unless a session's sign-in is recent. API keys are exempt.
    A session approved with a device code is refused outright: if someone was tricked into approving
    it, it must not mint keys or change how anyone signs in."""
    if principal.credential != SESSION:
        return
    if principal.via_device:
        raise HTTPException(status_code=403, detail={"code": "browser_sign_in_required", "message": "This needs a sign-in in a browser on this device, not a device code"})
    now = datetime.datetime.now(datetime.timezone.utc)
    if principal.authenticated_at is not None and now - principal.authenticated_at <= timedelta(seconds=STEP_UP_MAX_AGE):
        return
    raise HTTPException(
        status_code=401,
        detail={"code": "insufficient_user_authentication", "max_age": STEP_UP_MAX_AGE},
        headers={"WWW-Authenticate": f'Bearer error="insufficient_user_authentication", error_description="Recent sign-in required", max_age="{STEP_UP_MAX_AGE}"'},
    )


def require_human_admin_safeguard(principal: Principal, target, *, makes_admin: bool = False) -> None:
    """Changes to a human administrator, or that make someone one, need a person at a browser: an
    API key is refused, even the administrator's own, and a Hub session needs a recent sign-in."""
    if target is None or target.is_service_account:
        return
    if target.platform_role != PlatformRole.ADMIN.value and not makes_admin:
        return
    require_recent_hub_session(principal, "Changing a human administrator needs a Hub session, not an API key")


def require_recent_hub_session(principal: Principal, message: str) -> None:
    """For actions that need a person at a browser: API keys get 403, and a Hub session needs a recent sign-in."""
    if principal.credential != SESSION:
        raise HTTPException(status_code=403, detail={"code": "hub_session_required", "message": message})
    require_recent_authentication(principal)


def require_credential_change(principal: Principal, *, target) -> None:
    """Who may change how `target` signs in. Your own: a browser session with a recent sign-in, never
    an API key or a device-code session. Someone else's: a platform administrator, whose API key works
    for ordinary people, while any human administrator's needs a browser session with a recent sign-in."""
    if principal.is_user(target):
        require_recent_hub_session(principal, "Changing how you sign in needs a browser session with a recent sign-in, not an API key")
        return
    require_platform_admin(principal)
    require_recent_authentication(principal)
    require_human_admin_safeguard(principal, target)


def require_self_or_platform_admin(principal: Principal, user) -> None:
    """Raise 403 unless the caller is `user`, a User row or None, or a platform admin."""
    if principal.is_user(user):
        return
    require_platform_admin(principal)


def caller_row(session, principal: Principal, *, for_update: bool = False):
    """The caller's own User row, found by internal key."""

    if principal.internal_user_id is None:
        return None
    query = select(User).where(User.id == principal.internal_user_id)
    return session.exec(query.with_for_update() if for_update else query).one_or_none()


def is_caller(column, principal: Principal):
    """SQL test that `column` holds the caller's internal key. False for a caller without one,
    where `column == None` would match every row with no person."""
    from sqlalchemy import false

    return column == principal.internal_user_id if principal.internal_user_id is not None else false()


def _own_identity_group_ids(principal: Principal):
    from app.tables import UserAccessGroupLink

    return select(UserAccessGroupLink.accessgroup_id).where(
        is_caller(UserAccessGroupLink.user_id, principal),
        UserAccessGroupLink.role == Role.OWNER.db_value,
    )


def resolve_access_principal(
    session,
    caller: Principal,
    user_ref: Optional[str],
    *,
    allow_global_contributor: bool = False,
) -> Principal:
    if user_ref is None:
        return caller

    from app.utils.user_utils import resolve_user_by_identifier

    user = resolve_user_by_identifier(session, user_ref)
    if not (
        allow_global_contributor
        and has_global_permission(caller, Permission.READ)
    ):
        require_self_or_platform_admin(caller, user)
    if caller.is_user(user):
        return caller
    return load_principal(session, user.id)


def access_selector_filter(session, caller: Principal, resource_model,
                           user_refs: Optional[Sequence[str]] = None,
                           project_refs: Optional[Sequence[str]] = None,
                           max_selectors: int = 10):
    from app.tables import AccessGroup
    from app.utils.permissions import AccessGroupType
    from app.utils.project_utils import resolve_project_reference

    requested_users = list(user_refs or ())
    requested_projects = list(project_refs or ())
    if len(requested_users) + len(requested_projects) > max_selectors:
        raise HTTPException(
            status_code=422,
            detail=f"At most {max_selectors} access selectors may be provided",
        )

    filters = [permission_filter(caller, resource_model)]
    seen = set()

    for user_ref in requested_users:
        principal = resolve_access_principal(
            session,
            caller,
            user_ref,
            allow_global_contributor=True,
        )
        key = ("user", principal.user_id)
        if key in seen:
            continue
        seen.add(key)
        filters.append(permission_filter(principal, resource_model))

    for project_ref in requested_projects:
        project = resolve_project_reference(project_ref, session)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project '{project_ref}' not found")
        if (
            not has_global_permission(caller, Permission.READ)
            and caller.role_in(project.unique_id) is None
        ):
            raise HTTPException(status_code=403, detail="Not authorized to inspect project access")
        key = ("project", project.unique_id)
        if key in seen:
            continue
        seen.add(key)
        group = session.exec(
            select(AccessGroup).where(
                AccessGroup.group_name == project.unique_id,
                AccessGroup.group_type == AccessGroupType.PROJECT.value,
            )
        ).one_or_none()
        if group is None:
            raise HTTPException(
                status_code=409,
                detail=f"Project '{project.project_id}' has no project access group",
            )
        filters.append(access_group_permission_filter(group.id, resource_model))

    return and_(*filters)
