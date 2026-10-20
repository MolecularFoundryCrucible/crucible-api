from datetime import datetime, date, timedelta, timezone
from typing import Annotated, Dict, List, Optional, Union
import logging
from dataclasses import replace

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, func, join, or_
from sqlalchemy.orm import selectinload
from sqlmodel import Session, select, col

from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.api.path_params import DatasetMfid, UserRef
from app.general_utils import is_orcid, is_valid_mfid
from app.models.core_models import (
    AccessGroup, ApiToken, Dataset, Project, Resource, Sample, ScientificMetadataBinary, User, UserAccessGroupLink, get_mfid_value,
)
from app.utils.auth_utils import API_KEY_READ_DEPRECATION, authorize_user, fetch_acl_rows, find_or_add_api_access, has_active_api_key, permission_filter, require_recent_authentication, require_platform_admin, require_self_or_platform_admin, resolve_access_principal, _invalidate_auth_cache
from app.utils.cursor_utils import (
    CursorError, cursor_primary_value, filter_fingerprint, new_cursor, validate_cursor,
)
from app.utils.permissions import AccessGroupType, Permission, effective_role, has_global_permission, PlatformRole, Principal, Role
from app.utils.search_utils import (
    MAX_SEARCH_QUERY_LENGTH,
    SEARCH_QUERY_LIMIT_DESCRIPTION,
    apply_trigram_search,
    configure_trigram_search,
    normalize_search_query,
    trigram_rank,
)
from app.utils.sql_utils import assert_not_pending_deletion, deletion_filter_for, get_active_deletion_request, select_active, table_obj_to_dict
from app.utils.user_utils import assert_profile_fields_available, commit_user, find_mf_user_proposals, resolve_user_by_identifier, sql_add_user_to_access_group
from app.utils.project_utils import resolve_project_reference, sql_get_projects_for_user
from app.models.pagination import PaginatedResponse
from app.services.resource_pagination import execute_resource_page, prepare_resource_pagination
from app.api.routes.projects import PROJECT_PAGINATION
from app.models.users import UserCreate, UserPublicRead, UserRead, UserUpdate
from app.models.response_schemas import ApiKeyRead, ApiKeyVerify, DetailResponse
from app.models.core_models import ApiToken
from app.models.projects import ProjectWithLead
from app.models.access_groups import AccessGroupRead
from app.models.access import EffectiveAccess, EffectiveResourceAccessRead
from app.models.query_params import SortDirection, UserListQuery, UserProjectListQuery, UserSearchField, UserSort
from app.models.users import (
    AccountCapabilities,
    ServiceAccountKeyStatus,
    UserCapabilities,
    UserPlatformRoleUpdate,
    UserPublicRead,
    UserRead,
)

router = APIRouter(tags=["users"])
engine = get_engine()
USER_PROJECT_PAGINATION = replace(PROJECT_PAGINATION, kind="user_project")

USER_COLLECTION_DEPRECATION = "@1799280000"
API_KEY_VERIFY_DEPRECATION = "@1792368000"
USER_LIST_DECLARED_PARAMS = {
    "unique_id", "username", "email", "permissive", "is_service_account",
    "first_name", "last_name", "q", "search_fields", "platform_role", "include_total",
    "limit", "cursor", "offset", "sort", "direction", "active_since", "inactive_since", "never_active",
}


def _key_status(token: Optional[ApiToken]) -> Optional[ServiceAccountKeyStatus]:
    if token is None:
        return None
    created_at = token.token_created
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if isinstance(created_at, date) and not isinstance(created_at, datetime):
        created_at = datetime.combine(created_at, datetime.min.time())
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    expires_at = created_at + timedelta(days=365)
    return ServiceAccountKeyStatus(
        valid=expires_at >= datetime.now(timezone.utc),
        created_at=created_at.date().isoformat(),
        expires_at=expires_at.date().isoformat(),
        last_used_at=token.last_used_at,
    )


def _user_capabilities(caller: Principal, user: User) -> UserCapabilities:
    caller_is_admin = caller.is_platform_admin
    is_self = caller.user_id == user.unique_id
    return UserCapabilities(
        can_view_private=is_self or caller_is_admin,
        can_edit_profile=is_self or caller_is_admin,
        can_manage_api_key=caller_is_admin,
        can_change_platform_role=(
            caller_is_admin
            and not caller.is_service_account
            and not bool(user.is_service_account)
            and not is_self
        ),
    )


def _user_read(caller: Principal, user: User, *, key_status: Optional[ServiceAccountKeyStatus] = None,
               with_capabilities: bool = False) -> UserRead:
    role = None
    if user.platform_role is not None:
        try:
            role = PlatformRole(user.platform_role)
        except ValueError:
            role = None
    return UserRead.model_validate(
        user,
        update={
            "platform_role": role,
            "user_capabilities": _user_capabilities(caller, user) if with_capabilities else None,
            "api_key_status": key_status,
        },
    )


def _user_public_read(caller: Principal, user: User, *, with_capabilities: bool = False) -> UserPublicRead:
    return UserPublicRead.model_validate(
        user,
        update={"user_capabilities": _user_capabilities(caller, user) if with_capabilities else None},
    )


_USER_SEARCH_COLUMNS = {
    UserSearchField.FIRST_NAME: User.first_name,
    UserSearchField.LAST_NAME: User.last_name,
    UserSearchField.USERNAME: User.username,
    UserSearchField.UNIQUE_ID: User.unique_id,
    UserSearchField.EMAIL: User.email,
}


def _user_sort_key(sort: UserSort):
    if sort is UserSort.NAME:
        return func.lower(User.last_name) + " " + func.lower(User.first_name)
    if sort is UserSort.USERNAME:
        return func.lower(User.username)
    if sort is UserSort.EMAIL:
        return func.lower(User.email)
    if sort is UserSort.LAST_ACTIVE:
        return User.last_active_at
    return func.lower(User.unique_id)


def _user_sort_value(sort: UserSort, user: User):
    if sort is UserSort.NAME:
        if user.last_name is None or user.first_name is None:
            return None
        return f"{user.last_name.lower()} {user.first_name.lower()}"
    if sort is UserSort.LAST_ACTIVE:
        return user.last_active_at
    raw = {
        UserSort.USERNAME: user.username,
        UserSort.EMAIL: user.email,
        UserSort.UNIQUE_ID: user.unique_id,
    }[sort]
    return raw.lower() if raw is not None else None


@router.get("/users", response_model=PaginatedResponse[Union[UserRead, UserPublicRead]])
def list_users(auth_res: Annotated[Principal, Depends(authorize_user)], request: Request,
               response: Response,
               params: Annotated[UserListQuery, Query()]):
    '''
    Returns a paginated list of users. Platform administrators receive the full
    directory with email and platform roles; other callers receive public-safe
    profiles scoped to users who share an access group with them.
    Exact unique_id, username, and email filters return the same authorized
    representation as GET /users/{user_ref}. Any other query parameter is ignored
    and reported through the Deprecation response header.
    '''
    query_params = set(request.query_params.keys())
    ignored = sorted(query_params - USER_LIST_DECLARED_PARAMS)
    if ignored:
        response.headers["Deprecation"] = USER_COLLECTION_DEPRECATION
        logger.warning("Ignoring undeclared GET /users query parameters: %s", ", ".join(ignored))

    is_admin = auth_res.is_platform_admin
    if params.platform_role and not is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators may filter users by platform_role",
        )
    if not is_admin and (UserSearchField.EMAIL in (params.search_fields or []) or params.sort is UserSort.EMAIL):
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators may filter or sort users by email",
        )
    if not is_admin and (params.sort is UserSort.LAST_ACTIVE or params.active_since or params.inactive_since or params.never_active is not None):
        raise HTTPException(
            status_code=403,
            detail="Only platform administrators may filter or sort users by activity",
        )
    sort = params.sort or UserSort.NAME

    exact_lookup = params.unique_id is not None or params.username is not None or params.email is not None
    filters_digest = filter_fingerprint(params.cursor_filters())
    decoded_cursor = None
    decoded_primary = None
    if params.cursor is not None:
        try:
            decoded_cursor = validate_cursor(
                params.cursor,
                kind="user",
                sort=sort.value,
                direction=params.direction.value,
                filters=filters_digest,
                user_id=auth_res.user_id,
            )
            if decoded_cursor is not None:
                decoded_primary = cursor_primary_value(decoded_cursor)
        except CursorError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    search_cmd = select(User)
    if params.unique_id is not None:
        search_cmd = search_cmd.where(User.unique_id == params.unique_id)
    if params.username is not None:
        search_cmd = search_cmd.where(func.lower(User.username) == params.username.lower())
    if params.email is not None:
        search_cmd = search_cmd.where(func.lower(User.email) == params.email.lower())

    if params.first_name is not None:
        if params.permissive:
            search_cmd = search_cmd.where(User.first_name.ilike(f"%{params.first_name.lower()}%"))
        else:
            search_cmd = search_cmd.where(User.first_name == params.first_name)
    if params.last_name is not None:
        if params.permissive:
            search_cmd = search_cmd.where(User.last_name.ilike(f"%{params.last_name.lower()}%"))
        else:
            search_cmd = search_cmd.where(User.last_name == params.last_name)

    if params.q is not None:
        needle = f"%{params.q.lower()}%"
        fields = params.search_fields or [
            field for field in UserSearchField
            if is_admin or field is not UserSearchField.EMAIL
        ]
        search_cmd = search_cmd.where(or_(*(
            _USER_SEARCH_COLUMNS[field].ilike(needle) for field in dict.fromkeys(fields)
        )))

    if params.is_service_account is not None:
        search_cmd = search_cmd.where(User.is_service_account == params.is_service_account)

    if params.active_since is not None:
        search_cmd = search_cmd.where(User.last_active_at >= params.active_since)
    if params.inactive_since is not None:
        search_cmd = search_cmd.where(or_(User.last_active_at.is_(None), User.last_active_at < params.inactive_since))
    if params.never_active is not None:
        search_cmd = search_cmd.where(User.last_active_at.is_(None) if params.never_active else User.last_active_at.is_not(None))

    if params.platform_role:
        search_cmd = search_cmd.where(User.platform_role.in_(
            [role.value for role in params.platform_role]
        ))

    if not exact_lookup and not is_admin:
        ag_ids = [ag.id for ag in auth_res.groups]
        search_cmd = search_cmd.where(
            User.id.in_(
                select(UserAccessGroupLink.user_id).where(
                    UserAccessGroupLink.accessgroup_id.in_(ag_ids)
                )
            )
        )

    sort_key = _user_sort_key(sort)

    with Session(engine) as session:
        total = (
            session.exec(select(func.count()).select_from(search_cmd.subquery())).one()
            if params.include_total is not False
            else None
        )

        page_cmd = search_cmd
        ascending = params.direction is SortDirection.ASC
        if decoded_cursor is not None:
            after_id = User.unique_id > decoded_cursor.unique_id if ascending else User.unique_id < decoded_cursor.unique_id
            if decoded_cursor.is_null:
                page_cmd = page_cmd.where(sort_key.is_(None), after_id)
            else:
                beyond = sort_key > decoded_primary if ascending else sort_key < decoded_primary
                page_cmd = page_cmd.where(or_(
                    beyond,
                    and_(sort_key == decoded_primary, after_id),
                    sort_key.is_(None),
                ))
        if ascending:
            page_cmd = page_cmd.order_by(sort_key.asc().nulls_last(), User.unique_id.asc())
        else:
            page_cmd = page_cmd.order_by(sort_key.desc().nulls_last(), User.unique_id.desc())

        if params.cursor is None and params.offset:
            page_cmd = page_cmd.offset(params.offset)
        rows = session.exec(page_cmd.limit(params.limit + 1)).all()
        has_more = len(rows) > params.limit
        rows = rows[:params.limit]

        key_status_by_user = {}
        if is_admin and exact_lookup:
            private_ids = [user.unique_id for user in rows]
            for token in session.exec(
                select(ApiToken).where(ApiToken.user_unique_id.in_(private_ids))
                .order_by(ApiToken.token_created.desc())
            ).all() if private_ids else []:
                if token.user_unique_id not in key_status_by_user:
                    key_status_by_user[token.user_unique_id] = _key_status(token)
        items = [
            _user_read(auth_res, user, key_status=key_status_by_user.get(user.unique_id),
                       with_capabilities=exact_lookup)
            if is_admin or user.unique_id == auth_res.user_id
            else _user_public_read(auth_res, user, with_capabilities=exact_lookup)
            for user in rows
        ]
        next_cursor = None
        if has_more:
            last = rows[-1]
            next_cursor = new_cursor(
                kind="user",
                sort=sort.value,
                direction=params.direction.value,
                primary=_user_sort_value(sort, last),
                unique_id=last.unique_id,
                filters=filters_digest,
                user_id=auth_res.user_id,
            )
        return PaginatedResponse(
            total=total,
            limit=params.limit,
            offset=params.offset if params.cursor is None else None,
            items=items,
            next_cursor=next_cursor,
        )


@router.post("/users/resolve", response_model=Dict[str, UserPublicRead])
def resolve_users(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    orcids: Optional[List[str]] = Body(default=None),
    usernames: Optional[List[str]] = Body(default=None),
    emails: Optional[List[str]] = Body(default=None),
):
    '''
    Batch-resolve users by any mix of ORCIDs, usernames, or emails.
    `orcids` is matched against unique_id, so it also accepts service-account
    MFIDs; the response is keyed by unique_id. No email included.
    Any authenticated user can call this. Admins needing email should use GET /users/{user_ref}.
    '''
    if not orcids and not usernames and not emails:
        return {}

    with Session(engine) as session:
        results: Dict[str, UserPublicRead] = {}

        if orcids:
            for user in session.exec(select(User).where(User.unique_id.in_(orcids))).all():
                results[user.unique_id] = UserPublicRead.model_validate(user)

        if usernames:
            normalized_usernames = [u.lower() for u in usernames]
            for user in session.exec(select(User).where(User.username.in_(normalized_usernames))).all():
                if user.unique_id not in results:
                    results[user.unique_id] = UserPublicRead.model_validate(user)

        if emails:
            normalized_emails = [e.lower() for e in emails]
            for user in session.exec(select(User).where(func.lower(User.email).in_(normalized_emails))).all():
                if user.unique_id not in results:
                    results[user.unique_id] = UserPublicRead.model_validate(user)

        return results


@router.get("/users/search", response_model=List[UserPublicRead])
def search_users(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: str = Query(
        min_length=3,
        max_length=MAX_SEARCH_QUERY_LENGTH,
        description=(
            "Search term matched against username, first name, and last name. "
            f"{SEARCH_QUERY_LIMIT_DESCRIPTION}"
        ),
    ),
    is_service_account: Optional[bool] = Query(
        default=None,
        description="Restrict results to service accounts or human users.",
    ),
    limit: int = Query(default=20, ge=1, le=50),
):
    '''
    Search users by username, first name, or last name. Returns public profiles only (no email).
    Minimum query length: 3 characters. Maximum results: 50.
    '''
    normalized_q, tokens = normalize_search_query(q)
    with Session(engine) as session:
        configure_trigram_search(session, get_trgm_search_threshold())
        fields = (User.first_name, User.last_name, User.username)
        query = apply_trigram_search(
            select(User),
            tokens,
            fields=fields,
            prefix_fields=fields,
        )
        if is_service_account is not None:
            query = query.where(User.is_service_account == is_service_account)
        results = session.exec(
            query.order_by(
                trigram_rank(normalized_q, fields).desc(),
                User.unique_id.desc(),
            )
            .limit(limit)
        ).all()
    return results


@router.get("/users/by-username/{username}", response_model=Union[UserRead, UserPublicRead], deprecated=True)
def get_user_by_username(username: str, response: Response,
                         auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated: use GET /users/{user_ref}. Email is visible only to self and platform administrators.'''
    with Session(engine) as session:
        user = session.exec(select(User).where(User.username == username.lower())).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        response.headers["Content-Location"] = f"/users/{user.unique_id}"
        if auth_res.user_id == user.unique_id or auth_res.is_platform_admin:
            key_status = _admin_key_status(session, auth_res, user)
            return _user_read(auth_res, user, key_status=key_status, with_capabilities=True)
        return _user_public_read(auth_res, user, with_capabilities=True)


def _canonical_user_id(user_ref: str) -> str:
    """Map a username or email path segment to the user's unique_id. ORCIDs,
    MFIDs, and unresolvable values pass through so the route's own exact
    lookup and error handling apply unchanged."""
    if is_orcid(user_ref) or is_valid_mfid(user_ref):
        return user_ref
    with Session(engine) as session:
        try:
            return resolve_user_by_identifier(session, user_ref).unique_id
        except HTTPException:
            return user_ref


@router.get("/users/{user_ref}", response_model=Union[UserRead, UserPublicRead])
def read_user(user_ref: UserRef, response: Response,
              auth_res: Annotated[Principal, Depends(authorize_user)]):
    with Session(engine) as session:
        user = resolve_user_by_identifier(session, user_ref)
        is_admin = auth_res.is_platform_admin
        if user_ref != user.unique_id:
            response.headers["Content-Location"] = f"/users/{user.unique_id}"
        if auth_res.user_id == user.unique_id or is_admin:
            key_status = _admin_key_status(session, auth_res, user)
            return _user_read(auth_res, user, key_status=key_status, with_capabilities=True)
        return _user_public_read(auth_res, user, with_capabilities=True)


def _admin_key_status(session, caller: Principal, user: User) -> Optional[ServiceAccountKeyStatus]:
    if not caller.is_platform_admin:
        return None
    token = session.exec(
        select(ApiToken)
        .where(ApiToken.user_unique_id == user.unique_id)
        .order_by(ApiToken.token_created.desc())
    ).first()
    return _key_status(token)


def _admin_user_for_key(user_ref, auth_res):
    user_id = _canonical_user_id(user_ref)
    require_platform_admin(auth_res)
    require_recent_authentication(auth_res)
    with Session(engine) as session:
        if session.exec(select(User.id).where(User.unique_id == user_id)).first() is None:
            raise HTTPException(status_code=404, detail="User not found")
    return user_id


@router.post("/users/{user_ref}/apikey", response_model=ApiKeyRead, deprecated=True, responses={401: {"description": "Hub sessions need a recent ORCID sign-in (`insufficient_user_authentication`)"}, 409: {"description": "Hub sessions cannot read an existing key (`api_key_exists`)"}})
def get_or_provision_user_apikey(
    user_ref: UserRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    response: Response,
):
    '''Return the user's API key, minting one if they don't have it yet. Admin only. 404 if the user doesn't exist.

    Deprecated: returning an existing key will stop when keys are stored hashed. Use
    POST /users/{user_ref}/apikey/regenerate. Hub sessions already receive 409 when a key exists.
    '''
    user_id = _admin_user_for_key(user_ref, auth_res)
    if auth_res.credential == "hub" and has_active_api_key(user_id, engine):
        raise HTTPException(status_code=409, detail={"code": "api_key_exists", "message": "The user already has an API key; regenerate it instead"})
    response.headers["Cache-Control"] = "no-store"
    response.headers["Deprecation"] = API_KEY_READ_DEPRECATION
    return ApiKeyRead(api_key=find_or_add_api_access(user_id, engine, regenerate=False))


@router.post("/users/{user_ref}/apikey/regenerate", response_model=ApiKeyRead, responses={401: {"description": "Hub sessions need a recent ORCID sign-in (`insufficient_user_authentication`)"}})
def regenerate_user_apikey(
    user_ref: UserRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    response: Response,
):
    '''Issue a new API key for the user and revoke the previous one. Admin only. The key is returned only once.'''
    user_id = _admin_user_for_key(user_ref, auth_res)
    response.headers["Cache-Control"] = "no-store"
    return ApiKeyRead(api_key=find_or_add_api_access(user_id, engine, regenerate=True))


@router.get("/users/{user_ref}/apikey/verify", response_model=ApiKeyVerify, deprecated=True)
def verify_user_apikey(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)], response: Response):
    '''Returns validity and expiry info for the given user's API key. Admin only.

    Deprecated: administrators get the same status as `api_key_status` from GET /users/{user_ref}.
    '''
    user_id = _canonical_user_id(user_ref)
    require_platform_admin(auth_res)
    response.headers["Deprecation"] = API_KEY_VERIFY_DEPRECATION

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        status = _admin_key_status(session, auth_res, user)
        if status is None:
            raise HTTPException(status_code=404, detail="No API key found for this user")
        return ApiKeyVerify(**status.model_dump())

@router.post("/users", response_model=UserRead, responses={409: {"description": "Identifier, username, or email already in use; username and email conflicts use `{\"detail\": {\"code\": \"already_in_use\", \"fields\": [...], \"message\": ...}}`"}})
def create_user(auth_res: Annotated[Principal, Depends(authorize_user)], user_info: UserCreate,
                project_ids: Optional[List[str]] = Body(default=None)):
    require_platform_admin(auth_res)

    missing_fields = []
    if not user_info.first_name or not user_info.first_name.strip():
        missing_fields.append("first_name")
    if not user_info.last_name or not user_info.last_name.strip():
        missing_fields.append("last_name")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required user information: {', '.join(missing_fields)}."
        )

    unique_id = user_info.unique_id.strip() if user_info.unique_id else get_mfid_value()
    explicit_project_ids = set(project_ids or [])
    discovered_project_ids = set(find_mf_user_proposals(unique_id)) if is_orcid(unique_id) else set()
    requested_project_ids = explicit_project_ids | discovered_project_ids

    with Session(engine) as session:
        if session.exec(select(User).where(User.unique_id == unique_id)).one_or_none() is not None:
            raise HTTPException(status_code=409, detail=f"unique_id '{unique_id}' already exists")
        if session.exec(select(AccessGroup).where(AccessGroup.group_name == unique_id)).one_or_none() is not None:
            raise HTTPException(status_code=409, detail=f"Identity group '{unique_id}' already exists")
        assert_profile_fields_available(session, username=user_info.username or None, email=user_info.email)

        projects_by_mfid = {}
        missing_explicit = []
        for ref in sorted(requested_project_ids):
            project = resolve_project_reference(ref, session)
            if project is None:
                if ref in explicit_project_ids:
                    missing_explicit.append(ref)
                else:
                    logger.warning("Ignoring proposal project not provisioned in Crucible: %s", ref)
                continue
            if ref in explicit_project_ids:
                assert_not_pending_deletion(session, project.unique_id)
            elif get_active_deletion_request(session, project.unique_id) is not None:
                logger.warning("Ignoring proposal project pending deletion: %s", ref)
                continue
            projects_by_mfid[project.unique_id] = project
        if missing_explicit:
            raise HTTPException(
                status_code=404,
                detail=f"Projects not found: {', '.join(missing_explicit)}",
            )
        projects = list(projects_by_mfid.values())

        project_group_names = {project.unique_id for project in projects}
        project_groups = session.exec(
            select(AccessGroup).where(
                AccessGroup.group_name.in_(project_group_names),
                AccessGroup.group_type == AccessGroupType.PROJECT.value,
            )
        ).all() if project_group_names else []
        groups_by_name = {group.group_name: group for group in project_groups}
        missing_project_groups = sorted(project.project_id for project in projects if project.unique_id not in groups_by_name)
        if missing_project_groups:
            raise HTTPException(
                status_code=409,
                detail=f"Projects have no access group: {', '.join(missing_project_groups)}",
            )

        user_dict = table_obj_to_dict(user_info)
        user_dict["unique_id"] = unique_id
        user_dict["is_service_account"] = False
        if user_dict.get('email', None) is not None:
            user_dict['email'] = user_dict['email'].lower().strip()
        user = User(**user_dict)
        session.add(user)
        identity_group = AccessGroup(
            group_name=unique_id,
            group_type=AccessGroupType.IDENTITY.value,
        )
        session.add(identity_group)
        session.flush()
        session.add(UserAccessGroupLink(
            user_id=user.id,
            accessgroup_id=identity_group.id,
            role=Role.OWNER.db_value,
        ))
        session.add_all([
            UserAccessGroupLink(
                user_id=user.id,
                accessgroup_id=groups_by_name[project.unique_id].id,
                role=Role.CONTRIBUTOR.db_value,
            )
            for project in projects
        ])
        session.commit()
        session.refresh(user)

    # Mirror the canonical /auth/apikey flow: provisioning a user also mints
    # their API key (idempotent) so they can authenticate immediately.
    find_or_add_api_access(unique_id, engine, regenerate=False)
    return _user_read(auth_res, user, with_capabilities=True)


@router.patch("/users/{user_ref}", response_model=UserRead, responses={409: {"description": "Username or email already held by another user: `{\"detail\": {\"code\": \"already_in_use\", \"fields\": [\"username\", \"email\"], \"message\": ...}}`, listing every conflicting field"}})
def update_user(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)], user_update: UserUpdate):
    '''Partially update a user record. Only fields provided in the request body are modified.'''
    user_id = _canonical_user_id(user_ref)
    require_self_or_platform_admin(auth_res, user_id)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        update_data = user_update.model_dump(exclude_unset=True)
        assert_profile_fields_available(session, username=update_data.get("username"), email=update_data.get("email"), exclude_orcid=user_id)
        for key, value in update_data.items():
            setattr(user, key, value)

        session.add(user)
        commit_user(session)
        session.refresh(user)
        return _user_read(auth_res, user, with_capabilities=True)


@router.patch("/users/{user_ref}/platform_role", response_model=UserRead)
def update_user_platform_role(
    user_ref: UserRef,
    body: UserPlatformRoleUpdate,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''Change a human user's platform role. Human platform administrators only.'''
    require_platform_admin(auth_res)
    if auth_res.is_service_account:
        raise HTTPException(
            status_code=403,
            detail="Service accounts cannot manage platform roles",
        )

    with Session(engine) as session:
        user = resolve_user_by_identifier(session, user_ref)
        if user.is_service_account:
            raise HTTPException(
                status_code=409,
                detail=(f"{user.unique_id} is a service account. "
                        f"Use PATCH /service_accounts/{user.unique_id} instead."),
            )
        if user.unique_id == auth_res.user_id:
            raise HTTPException(status_code=403, detail="Callers cannot change their own platform role")

        if user.platform_role == PlatformRole.ADMIN.value and body.platform_role != "admin":
            # Lock every human admin row, not just the target, so two concurrent
            # demotions cannot each count the other as the remaining admin.
            human_admin_ids = session.exec(
                select(User.id)
                .where(
                    User.platform_role == PlatformRole.ADMIN.value,
                    User.is_service_account == False,
                )
                .order_by(User.id)
                .with_for_update()
            ).all()
            if user.id in human_admin_ids and len(human_admin_ids) <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="Cannot demote the last human platform administrator",
                )

        user.platform_role = body.platform_role
        session.add(user)
        session.commit()
        session.refresh(user)
        key_status = _admin_key_status(session, auth_res, user)
        return _user_read(auth_res, user, key_status=key_status, with_capabilities=True)


@router.delete("/users/{user_ref}/apikey", status_code=204)
def delete_user_apikey(
    user_ref: UserRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''Revoke every API key held by the user. Platform administrators only. 204 on success.'''
    user_id = _canonical_user_id(user_ref)
    require_platform_admin(auth_res)

    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        tokens = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == user_id)
        ).all()
        for token in tokens:
            session.delete(token)
        session.commit()

    for token in tokens:
        _invalidate_auth_cache(token.token)
    return Response(status_code=204)


@router.get("/users/{user_ref}/datasets", response_model=List[str], deprecated=True)
def read_user_datasets(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''
    Deprecated compatibility route returning dataset MFIDs. Use GET /datasets?accessible_to_user={user_ref}.
    '''
    with Session(engine) as session:
        access_principal = resolve_access_principal(session, auth_res, user_ref)
        return session.exec(
            select(Dataset.unique_id)
            .where(permission_filter(access_principal, Dataset))
            .where(deletion_filter_for(Dataset))
            .order_by(Dataset.unique_id.desc())
        ).all()


@router.get("/users/{user_ref}/samples", response_model=List[str], deprecated=True)
def read_user_samples(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Deprecated compatibility route returning sample MFIDs. Use GET /samples?accessible_to_user={user_ref}.'''
    with Session(engine) as session:
        access_principal = resolve_access_principal(session, auth_res, user_ref)
        return session.exec(
            select(Sample.unique_id)
            .where(permission_filter(access_principal, Sample))
            .where(deletion_filter_for(Sample))
            .order_by(Sample.unique_id.desc())
        ).all()


@router.get("/users/{user_ref}/datasets/{dataset_mfid}", response_model=EffectiveResourceAccessRead)
def read_user_access_to_dataset(dataset_mfid: DatasetMfid, user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Returns the user's effective role on one dataset. Limited to self and platform administrators.'''
    with Session(engine) as session:
        access_principal = resolve_access_principal(session, auth_res, user_ref)
        dataset = session.exec(select_active(Dataset, dataset_mfid)).one_or_none()
        if dataset is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        if access_principal.is_platform_admin:
            access = EffectiveAccess.PLATFORM_ADMIN
        elif has_global_permission(access_principal, Permission.READ):
            access = EffectiveAccess.CONTRIBUTOR
        else:
            role = effective_role(access_principal, fetch_acl_rows(session, dataset_mfid))
            if access_principal.is_service_account and role is not None:
                role = min(role, Role.CONTRIBUTOR)
            access = EffectiveAccess(role.db_value) if role is not None else EffectiveAccess.NONE

        return EffectiveResourceAccessRead(
            resource_mfid=dataset_mfid,
            user_id=access_principal.user_id,
            effective_access=access,
        )


@router.post(
    "/users/{user_ref}/access_groups/{group_name}",
    response_model=AccessGroupRead,
    deprecated=True,
)
def add_user_to_access_group(auth_res: Annotated[Principal, Depends(authorize_user)], user_ref: UserRef, group_name: str):
    '''Add a user to an access group. Not for project groups. Use
    POST /projects/{project_ref}/users/{user_ref}, which applies the project's own
    role cap instead of platform-admin-only.'''
    user_id = _canonical_user_id(user_ref)
    require_platform_admin(auth_res)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access group not found")
        if ag.group_type == AccessGroupType.IDENTITY.value:
            raise HTTPException(status_code=409, detail="Identity-group membership cannot be changed")
        if ag.group_type == AccessGroupType.SYSTEM.value:
            raise HTTPException(status_code=409, detail="System-group membership cannot be changed")
        if ag.group_type == AccessGroupType.PROJECT.value:
            raise HTTPException(
                status_code=400,
                detail=f"'{group_name}' is a project. Use POST /projects/{group_name}/users/{user_id} instead.")
        if ag.group_type == AccessGroupType.INSTRUMENT.value and not user.is_service_account:
            raise HTTPException(
                status_code=409,
                detail=(f"'{group_name}' is an instrument group, which holds service accounts only. "
                        f"Grant instrument maintainers access through PUT /resources/{group_name}/access/users/{user_id}."))
        ag = sql_add_user_to_access_group(user_id, group_name, session)
        session.commit()
        session.refresh(ag)
    return ag


@router.get("/users/{user_ref}/access_groups", response_model=List[str])
def list_user_access_groups(auth_res: Annotated[Principal, Depends(authorize_user)], user_ref: UserRef):
    user_id = _canonical_user_id(user_ref)
    require_self_or_platform_admin(auth_res, user_id)
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        user_ags = session.exec(
            select(AccessGroup)
            .join(UserAccessGroupLink, UserAccessGroupLink.accessgroup_id == AccessGroup.id)
            .where(UserAccessGroupLink.user_id == user.id)
        ).all()
        return([ag.group_name for ag in user_ags])


@router.delete(
    "/users/{user_ref}/access_groups/{group_name}",
    response_model=DetailResponse,
    deprecated=True,
)
def remove_user_from_access_group(auth_res: Annotated[Principal, Depends(authorize_user)], user_ref: UserRef, group_name: str):
    '''Remove a user from an access group. Not for project groups. Use
    DELETE /projects/{project_ref}/users/{user_ref}, which keeps membership records
    in step instead of only revoking access-group standing.'''
    user_id = _canonical_user_id(user_ref)
    require_platform_admin(auth_res)
    with Session(engine) as session:
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        if ag is None:
            raise HTTPException(status_code=404, detail="Access group not found")
        if ag.group_type == AccessGroupType.IDENTITY.value:
            raise HTTPException(status_code=409, detail="Identity-group membership cannot be changed")
        if ag.group_type == AccessGroupType.SYSTEM.value:
            raise HTTPException(status_code=409, detail="System-group membership cannot be changed")
        if ag.group_type == AccessGroupType.PROJECT.value:
            raise HTTPException(
                status_code=400,
                detail=f"'{group_name}' is a project. Use DELETE /projects/{group_name}/users/{user_id} instead.")

        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        link = session.exec(
            select(UserAccessGroupLink).where(
                UserAccessGroupLink.user_id == user.id,
                UserAccessGroupLink.accessgroup_id == ag.id
            )
        ).one_or_none()

        if link is None:
            raise HTTPException(status_code=404, detail="User is not a member of this access group")

        session.delete(link)
        session.commit()
        return {"detail": f"User {user_id} removed from access group {group_name}"}


@router.get("/users/{user_ref}/projects", response_model=PaginatedResponse[ProjectWithLead])
def read_projects_for_user(
    user_ref: UserRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
    params: Annotated[UserProjectListQuery, Query()],
    request: Request = None,
):
    '''
    Returns a paginated list of the projects the user is a member of, each with
    the user's standing role in that project's access group.
    Available to the user themselves and to callers with platform_role
    contributor or admin, the same rule as accessible_to_user on GET /projects.
    Membership only: unlike GET /projects?accessible_to_user=, projects reached
    only through explicit resource grants are not included.
    Pass role to restrict to the listed roles.
    Pass sort=project_id|title|created|updated with direction=asc|desc to change the order.
    Pass include_total=false to omit the exact count.
    '''
    user_id = _canonical_user_id(user_ref)
    with Session(engine) as session:
        if auth_res.user_id != user_id and not has_global_permission(auth_res, Permission.READ):
            raise HTTPException(status_code=403, detail="Not authorized")

        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        pagination = prepare_resource_pagination(
            params=params,
            query_params=request.query_params if request is not None else {},
            spec=USER_PROJECT_PAGINATION,
            result_filters={**params.normalized_result_filters(), "user_id": user_id},
            user_id=auth_res.user_id,
        )
        base = (
            select(Project, Resource, UserAccessGroupLink.role)
            .join(Resource, Resource.mfid == Project.unique_id, isouter=True)
            .join(AccessGroup, AccessGroup.group_name == Project.unique_id)
            .join(UserAccessGroupLink, and_(
                UserAccessGroupLink.accessgroup_id == AccessGroup.id,
                UserAccessGroupLink.user_id == user.id,
            ))
            .where(deletion_filter_for(Project))
        )
        if params.role:
            base = base.where(UserAccessGroupLink.role.in_([Role.from_db(r.value).db_value for r in params.role]))

        total = (
            session.exec(select(func.count()).select_from(base.with_only_columns(Project.id).subquery())).one()
            if params.include_total is not False
            else None
        )
        page = execute_resource_page(session, base, pagination)
        projects = [row[0] for row in page.rows]
        metadata_by_mfid = {}
        if params.include_metadata and projects:
            metadata_by_mfid = {
                smb.unique_id: smb.scientific_metadata
                for smb in session.exec(
                    select(ScientificMetadataBinary)
                    .where(ScientificMetadataBinary.unique_id.in_([p.unique_id for p in projects]))
                ).all()
            }
        orcids = [p.project_lead_orcid for p in projects if p.project_lead_orcid]
        leads = {u.unique_id: u for u in session.exec(select(User).where(User.unique_id.in_(orcids))).all()}
        items = [
            ProjectWithLead(
                **proj.model_dump(),
                lead=leads.get(proj.project_lead_orcid),
                creation_time=resource.creation_time if resource else None,
                modification_time=resource.modification_time if resource else None,
                scientific_metadata=metadata_by_mfid.get(proj.unique_id),
                role=role or "unknown",
            )
            for proj, resource, role in page.rows
        ]
        return PaginatedResponse(
            total=total,
            limit=params.limit,
            offset=params.offset if params.cursor is None else None,
            items=items,
            next_cursor=page.next_cursor,
        )
