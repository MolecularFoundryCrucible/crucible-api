from typing import Annotated, Optional
import logging
import re
import os
import hashlib
import threading
import requests
import secrets
import datetime
from collections import namedtuple
from cachetools import TTLCache
from jose import jwt, JWTError

logger = logging.getLogger(__name__)

from fastapi import Depends,  HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.general_utils import  get_secret
from app.api.dependencies import get_engine, get_api_path, get_api_url
from app.models.core_models import ApiToken
from app.utils.permissions import (
    PUBLIC_GROUP_NAME, THRESHOLDS, GroupMembership, Permission, PlatformRole,
    Principal, Role, effective_role)
from sqlmodel import Session, select
from sqlalchemy.sql import and_
from sqlalchemy import join
from datetime import date, timedelta

# ====== Constants
SECRET_KEY = get_secret("CRUCIBLE_SECRET_KEY", "crucible_secret_key/versions/1")
ALGORITHM = "HS256"
security = HTTPBearer()

engine = get_engine()
api_path = get_api_path()
api_url = get_api_url()

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


def _invalidate_auth_cache(apikey: str):
    """Drop a token from this worker's auth cache. Only affects the current
    process; other workers/pods clear the entry when their TTL expires."""
    with _auth_cache_lock:
        _auth_cache.pop(_auth_cache_key(apikey), None)


# ====== Functions
def find_or_add_api_access(orcid: str, engine, regenerate = False):

    # query the database for orcid
    one_year_ago = (date.today()-timedelta(365)).isoformat()
    with Session(engine) as session:
        api_acct = session.exec(select(ApiToken).where(and_(ApiToken.user_unique_id == orcid,
                                                            ApiToken.token_created > one_year_ago))).one_or_none()

        if api_acct is not None and regenerate == False:
            return(api_acct.token)

        elif api_acct is not None and regenerate == True:
            old_token = api_acct.token
            delete_apikey(orcid, session)
            new_token = generate_apikey(orcid, session)
            _invalidate_auth_cache(old_token)
            return(new_token)

        else:
            new_token = generate_apikey(orcid, session)
            return(new_token)


def generate_apikey(orcid: str, session):
    # create token
    new_token = secrets.token_hex(32)
    # add to database
    new_api_acct = ApiToken(user_unique_id = orcid,
                            access_group_id = orcid,
                            token = new_token,
                            token_created = date.today().isoformat())
    session.add(new_api_acct)
    session.commit()
    return(new_token)

    
def delete_apikey(orcid, session):
    session.query(ApiToken).filter(ApiToken.user_unique_id == orcid).delete(synchronize_session=False)
    session.commit()


def code_for_token_exchange(code: str, client_id, client_secret, redirect_uri):
    token_req = requests.post(url = "https://orcid.org/oauth/token", 
                              data = {  "client_id": client_id,
                                        "client_secret": client_secret, 
                                        "grant_type":"authorization_code", 
                                        "code":code, 
                                        "redirect_uri":redirect_uri
                                     }, 
                              headers = {'Accept': 'application/json'})   
    
    response_data = token_req.json()
    if 'access_token' not in response_data:
        raise HTTPException(
            status_code=401,
            detail=f"ORCID authentication failed: {response_data.get('error_description', response_data)}",
        )
    return response_data['access_token']


def decode_token(token: str, secret_key = SECRET_KEY, alg = ALGORITHM):
    try:
        payload = jwt.decode(token, secret_key, algorithms=[alg])
        return payload
    except JWTError:
        login_path = ''.join([api_path, '/login'])
        return RedirectResponse(login_path)


def find_key(credentials, engine):
    """
    Find an API token record in the database matching the provided credentials.
    
    Args:
        credentials: HTTPAuthorizationCredentials object containing the API token
        
    Returns:
        ApiToken record if found and not expired (created within last year), None otherwise
        
    The function checks if the provided API token exists in the database and was created
    within the last year. This is used for authenticating API requests.
    """
    apikey = credentials.credentials
    one_year_ago = (date.today()-timedelta(365)).isoformat()
    with Session(engine) as session:
        cmd = select(ApiToken).where(and_(ApiToken.token == apikey,
                                          ApiToken.token_created > one_year_ago))
        api_acct = session.exec(cmd).one_or_none()

        if api_acct is None:
            # Return an identical response whether the key never existed or has
            # expired, so the error can't be used to probe token existence or
            # leak the exact expiry date. Details go to the server log only.
            cmd = select(ApiToken).where(ApiToken.token == apikey)
            expired_api_acct = session.exec(cmd).one_or_none()
            if expired_api_acct is not None:
                logger.warning(
                    "Expired API key presented (user=%s, created=%s)",
                    expired_api_acct.user_unique_id, expired_api_acct.token_created,
                )
            raise HTTPException(status_code=401, detail="Invalid or expired API key.")

    return(api_acct)


def authorize_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> Principal:
    """Resolve the caller to a Principal. Raises 401 if the key is invalid."""
    return _authorize_user(credentials)


def _authorize_user(credentials: HTTPAuthorizationCredentials) -> Principal:
    apikey = credentials.credentials
    cache_key = _auth_cache_key(apikey)
    with _auth_cache_lock:
        cached = _auth_cache.get(cache_key)
    if cached is not None:
        return cached

    api_acct = find_key(credentials, engine)  # raises 401 on invalid/expired

    if api_acct is None:
        raise HTTPException(status_code=401, detail="Invalid or expired API key.")

    with Session(engine) as session:
        result = load_principal(session, api_acct.user_unique_id)

    with _auth_cache_lock:
        _auth_cache[cache_key] = result
    return result


def load_principal(session, orcid: str) -> Principal:
    """Build a Principal from the database. Split out of authorize_user so the
    query is testable without an API key or a live request."""
    from app.models.core_models import AccessGroup, User, UserAccessGroupLink

    user = session.exec(select(User).where(User.unique_id == orcid)).first()
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
        user_id=orcid,
        platform_role=_platform_role(user.platform_role),
        is_service_account=bool(user.is_service_account),
        groups=tuple(groups),
    )


def _public_group_id(session) -> Optional[int]:
    """Id of the virtual public group, cached for the process. None before the
    phase 1 migration, making injection a no-op rather than an error."""
    global _PUBLIC_GROUP_ID
    if _PUBLIC_GROUP_ID is _UNSET:
        from app.models.core_models import AccessGroup
        _PUBLIC_GROUP_ID = session.exec(
            select(AccessGroup.id).where(AccessGroup.group_name == PUBLIC_GROUP_NAME)
        ).first()
    return _PUBLIC_GROUP_ID


# ====== Role-based checks

def fetch_acl_rows(session, mfid: str):
    """(accessgroup_id, permission) for one resource."""
    from app.models.core_models import ResourceACL
    rows = session.exec(
        select(ResourceACL.accessgroup_id, ResourceACL.permission)
        .where(ResourceACL.mfid == mfid)
    ).all()
    return [(gid, Role.from_db(r)) for gid, r in rows if r is not None]


def require_permission(session, principal: Principal, mfid: str,
                       permission: Permission,
                       legacy_group: Optional[str] = None) -> Optional[Role]:
    """Raise 403 unless `principal` may perform `permission` on `mfid`.

    Returns the effective role, so a caller needing to branch further does not
    recompute it. None means a platform admin, whose access comes from bypass
    rather than an ACL entry.

    `legacy_group` names the pre-role group for resource types that only gained
    ACL entries in the phase 1 backfill — projects and instruments. Where the
    resource has no ACL rows at all, membership of that group is honoured
    instead, so deploying before the backfill does not lock the routes out.
    Drop it in phase 5.
    """
    from app.utils.permissions import has_permission

    if principal.is_platform_admin:
        return None

    acl_rows = fetch_acl_rows(session, mfid)

    if not acl_rows and legacy_group is not None:
        if any(g.group_name in ("admin", legacy_group) for g in principal.groups):
            return None
        raise HTTPException(status_code=403, detail="Not authorized")

    if not has_permission(principal, acl_rows, permission):
        raise HTTPException(status_code=403, detail="Access denied")
    return effective_role(principal, acl_rows)


def require_grant(session, principal: Principal, mfid: str, target: Role,
                  current: Optional[Role] = None, floor: Role = Role.ADMIN) -> None:
    """Raise 403 unless `principal` may set a grant on `mfid` to `target`.

    The cap binds on both ends — you may not grant above your own role, nor
    overwrite one above it. `floor` is the minimum role that may manage at all:
    admin for ACL entries, editor for project membership.
    """
    from app.utils.permissions import can_grant

    if principal.is_platform_admin:
        return

    actor = effective_role(principal, fetch_acl_rows(session, mfid))
    if not can_grant(actor, target, current, floor=floor):
        raise HTTPException(
            status_code=403,
            detail=(f"You hold {actor.name.lower() if actor else 'no role'} here "
                    f"and cannot grant {target.name.lower()}"))


def permission_filter(principal: Principal, resource_model,
                      permission: Permission = Permission.READ):
    """SQL filter restricting a list query to resources the caller may reach.

    Relies on min(a,b) >= r being equivalent to a >= r and b >= r: the standing
    side is filtered in Python, so only a single column comparison reaches SQL
    and useraccessgrouplink never has to be joined.
    """
    from sqlalchemy import exists, false, true
    from app.models.core_models import ResourceACL

    if principal.is_platform_admin:
        return true()

    threshold = THRESHOLDS[permission]
    group_ids = principal.group_ids_at_or_above(threshold)
    if not group_ids:
        return false()

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
            ResourceACL.mfid == resource_model.unique_id,
            ResourceACL.accessgroup_id.in_(group_ids),
            ResourceACL.permission.in_(allowed),
        )
        .select_from(ResourceACL)
        .correlate(resource_model)
    )


def has_platform_admin_access(principal: Principal) -> bool:
    return principal.is_platform_admin or any(
        group.group_name == "admin" for group in principal.groups
    )


def require_platform_admin(principal: Principal) -> None:
    """Raise 403 unless the caller has platform-wide bypass.

    Accepts admin-group membership as well as platform_role for now. On a
    database where the phase 1 backfill has not run, platform_role is 'none'
    for everyone, and checking it alone would lock every admin route with no
    recovery short of direct database access. Drop the group arm in phase 5.
    """
    if has_platform_admin_access(principal):
        return
    raise HTTPException(status_code=403, detail="Not authorized")


def require_self_or_platform_admin(principal: Principal, orcid: str) -> None:
    """Raise 403 unless the caller is `orcid` or a platform admin."""
    if principal.user_id == orcid:
        return
    require_platform_admin(principal)


def resolve_access_principal(session, caller: Principal, user_ref: Optional[str]) -> Principal:
    if user_ref is None:
        return caller

    from app.utils.user_utils import resolve_user_by_identifier

    user = resolve_user_by_identifier(session, user_ref)
    require_self_or_platform_admin(caller, user.unique_id)
    if caller.user_id == user.unique_id:
        return caller
    return load_principal(session, user.unique_id)


def is_instrument_group_member(principal: Principal, session) -> bool:
    """Whether the caller holds standing in any registered instrument's
    operator group — named after the instrument's MFID (unique_id).
    """
    from app.models.core_models import Instrument

    if not principal.groups:
        return False
    group_names = {g.group_name for g in principal.groups}
    return session.exec(
        select(Instrument).where(Instrument.unique_id.in_(group_names))
    ).first() is not None
