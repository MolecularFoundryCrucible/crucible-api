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
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.general_utils import  get_secret
from app.api.dependencies import get_engine, get_api_path, get_api_url
from app.models.core_models import ApiToken
from app.utils.permissions import (
    PUBLIC_GROUP_NAME, THRESHOLDS, GroupMembership, Permission, PlatformRole,
    Principal, Role, effective_role)
from app.utils import shadow
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


async def authorize_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
) -> Principal:
    """Resolve the caller to a Principal. Raises 401 if the key is invalid.

    Async purely so shadow.current_principal is set in the request's own
    context: FastAPI runs a *sync* dependency in a threadpool with a copied
    context, so a ContextVar set there never reaches the route handler. The
    blocking work stays off the event loop via run_in_threadpool.
    """
    principal = await run_in_threadpool(_authorize_user, credentials)
    shadow.current_principal.set(principal)
    return principal


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


def _admin_group_predicate(ag_ids):
    """SQL predicate: is any of `ag_ids` the `admin` group?

    Resolved per-query rather than from a cached id, which would answer for
    whichever database the process started against.
    """
    from sqlalchemy import exists
    from app.models.core_models import AccessGroup
    return exists().where(
        AccessGroup.id.in_(ag_ids),
        AccessGroup.group_name == 'admin',
    )


def _is_admin_by_group(session, ag_ids) -> bool:
    """Transitional bypass; removed with these helpers at cutover.

    Admin-group members currently reach everything through ~360k identical ACL
    rows. Honouring the membership directly makes those rows deletable without
    cutting off the service accounts that live in that group. Grants access to
    exactly the set that already has it.
    """
    from sqlalchemy import select as sa_select
    if not ag_ids:
        return False
    return bool(session.execute(sa_select(_admin_group_predicate(ag_ids))).scalar())


def require_read_access(session, mfid: str, ag_ids: list):
    """Raise 403 if the user has no read access to the given MFID.

    Access is granted if Resource.public is True OR the user has an ACL entry.
    Call after a 404 check so the caller can distinguish not-found from forbidden.
    """
    from sqlalchemy import exists, select
    from app.models.core_models import Resource, ResourceACL

    if _is_admin_by_group(session, ag_ids):
        shadow.compare(session, "require_read_access", mfid, Permission.READ, True)
        return

    acl_ok = session.exec(
        select(Resource).where(Resource.mfid == mfid).where(
            (Resource.public == True) |
            exists().where(
                ResourceACL.mfid == mfid,
                ResourceACL.accessgroup_id.in_(ag_ids)
            )
        )
    ).first()
    shadow.compare(session, "require_read_access", mfid, Permission.READ, bool(acl_ok))
    if not acl_ok:
        raise HTTPException(status_code=403, detail="Access denied")


def read_access_filter_list(resource_model, ag_ids: list):
    """
    Returns (auth_filter, deletion_filter) for list queries on a resource model.

    Requires the query to join Resource on Resource.mfid == resource_model.unique_id.
    auth_filter: visible if Resource.public is True OR user has an ACL entry.
    deletion_filter: excludes resources with a pending or approved deletion request.
    """
    from sqlalchemy import exists
    from app.models.core_models import Resource, ResourceACL
    from app.utils.sql_utils import deletion_filter_for

    acl_subquery = exists().where(
        ResourceACL.mfid == resource_model.unique_id,
        ResourceACL.accessgroup_id.in_(ag_ids)
    )
    # Admin-group membership short-circuits, evaluated by the database so no
    # cached group id can answer for the wrong one. See _is_admin_by_group.
    auth_filter = (
        _admin_group_predicate(ag_ids) | (Resource.public == True) | acl_subquery
    )
    return auth_filter, deletion_filter_for(resource_model)


def require_write_access(session, ag_ids: list, mfids: list):
    """Raise 403 if the user has no write ACL on any of the given MFIDs."""
    from app.models.core_models import ResourceACL
    from sqlmodel import select

    if _is_admin_by_group(session, ag_ids):
        for m in mfids:
            shadow.compare(session, "require_write_access", m, Permission.EDIT, True)
        return

    acl_ok = session.exec(
        select(ResourceACL).where(
            ResourceACL.mfid.in_(mfids),
            ResourceACL.accessgroup_id.in_(ag_ids),
            ResourceACL.write == True
        )
    ).first()
    # The old check passes if ANY mfid is writable, so the comparison is per
    # resource against that same aggregate answer — a per-mfid mismatch here
    # means the new model disagrees about that specific resource.
    for m in mfids:
        shadow.compare(session, "require_write_access", m, Permission.EDIT, bool(acl_ok))
    if not acl_ok:
        raise HTTPException(status_code=403, detail="Write access required to at least one of the resources")


def require_groups(ag_list, allowed_groups={'admin'}):
    if not any(ag.group_name in allowed_groups for ag in ag_list):
        raise HTTPException(status_code=403, detail="Not authorized")


def require_admin(ag_list):
    require_groups(ag_list, {'admin'})


def is_admin(credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)]):
    """
    Check if a user is an admin based on their API credentials.
    
    Args:
        credentials: HTTPAuthorizationCredentials object containing the API token
        
    Returns:
        "admin" if the user is an admin, raises HTTPException otherwise
        
    This function validates the provided API credentials and checks if the user belongs to the 'admin' access group.
    """
    principal = _authorize_user(credentials)

    if 'admin' in [ag.group_name for ag in principal.groups]:
        return('admin')



# ====== Role-based checks (phase 4)
# These replace require_read_access / require_write_access /
# read_access_filter_list / require_groups / require_admin at cutover.

def fetch_acl_rows(session, mfid: str):
    """(accessgroup_id, role) for one resource."""
    from app.models.core_models import ResourceACL
    rows = session.exec(
        select(ResourceACL.accessgroup_id, ResourceACL.role)
        .where(ResourceACL.mfid == mfid)
    ).all()
    return [(gid, Role.from_db(r)) for gid, r in rows if r is not None]


def require_permission(session, principal: Principal, mfid: str,
                       permission: Permission) -> Optional[Role]:
    """Raise 403 unless `principal` may perform `permission` on `mfid`.

    Returns the effective role, so a caller that needs to branch further does
    not have to recompute it. None means the caller is a platform admin, whose
    access does not come from an ACL entry.
    """
    from app.utils.permissions import has_permission

    if principal.is_platform_admin:
        return None

    acl_rows = fetch_acl_rows(session, mfid)
    if not has_permission(principal, acl_rows, permission):
        raise HTTPException(status_code=403, detail="Access denied")
    return effective_role(principal, acl_rows)


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
            ResourceACL.role.in_(allowed),
        )
        .select_from(ResourceACL)
        .correlate(resource_model)
    )


def require_platform_admin(principal: Principal) -> None:
    """Raise 403 unless the caller has platform-wide bypass.

    Accepts admin-group membership as well as platform_role for now. On a
    database where the phase 1 backfill has not run, platform_role is 'none'
    for everyone, and checking it alone would lock every admin route with no
    recovery short of direct database access. Drop the group arm in phase 5.
    """
    if principal.is_platform_admin:
        return
    if any(g.group_name == "admin" for g in principal.groups):
        return
    raise HTTPException(status_code=403, detail="Not authorized")
