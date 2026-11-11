from datetime import datetime, timezone
from typing import Annotated, Dict, List, Optional, Union
import logging
from dataclasses import replace

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from sqlalchemy import and_, func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.api.removed import REMOVED_RESPONSE, removed
from app.api.dependencies import get_engine, get_trgm_search_threshold
from app.api.path_params import DatasetMfid, UserRef
from app.general_utils import is_orcid, is_valid_mfid
from app.tables import (
    UserIdentity,
    AccessGroup, Dataset, Project, Resource, ScientificMetadataBinary, User, UserAccessGroupLink, get_mfid_value,
)
from app.utils.auth_utils import caller_row, forget_cached_keys, authorize_user, fetch_acl_rows, require_human_admin_safeguard, require_recent_authentication, require_platform_admin, require_self_or_platform_admin, resolve_access_principal
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
from app.utils.sql_utils import assert_not_pending_deletion, deletion_filter_for, select_active
from app.auth.api_keys import key_status, newest_key, newest_keys, replace_plain_keys, revoke_user_keys
from app.auth.events import AuthEventType, record_event
from app.auth.linking import sign_in_labels
from app.auth.password_links import void_links
from app.auth.registration import NotPending, StillReferenced, approve_pending_user, reject_pending_user
from app.auth.identity import IdentityTaken, default_orcid_issuer, find_identity
from app.auth.tokens import revoke_all_tokens
from app.services.proposals import find_mf_user_proposals, usable_discovered_projects
from app.utils.user_utils import assert_profile_fields_available, assert_username_kept_for_password, assert_self_chosen_username_allowed, commit_user, person_by_reference, profile_conflict, provision_user, resolve_user_by_identifier, sql_add_user_to_access_group
from app.utils.project_utils import project_fields, project_leads, resolve_project_reference
from app.models.pagination import PaginatedResponse
from app.services.resource_pagination import execute_resource_page, prepare_resource_pagination
from app.api.routes.projects import PROJECT_PAGINATION
from app.models.users import UserCreate, UserPublicRead, UserRead, UserUpdate
from app.models.response_schemas import ApiKeyVerify, DetailResponse
from app.models.projects import ProjectWithLead
from app.models.access_groups import AccessGroupRead
from app.models.access import EffectiveAccess, EffectiveResourceAccessRead
from app.models.query_params import SortDirection, UserListQuery, UserProjectListQuery, UserSearchField, UserSort
from app.models.users import (
    ServiceAccountKeyStatus,
    UserCapabilities,
    UserPlatformRoleUpdate,
)

router = APIRouter(tags=["users"])
USER_PROJECT_PAGINATION = replace(PROJECT_PAGINATION, kind="user_project")

IDENTITY_LINKED_ELSEWHERE = "This ORCID is already linked to another user"
USER_COLLECTION_DEPRECATION = "@1799280000"
API_KEY_VERIFY_DEPRECATION = "@1792368000"
USER_RESOLVE_DEPRECATION = "@1792540800"
USER_LIST_DECLARED_PARAMS = {
    "unique_id", "username", "email", "permissive", "is_service_account",
    "first_name", "last_name", "q", "search_fields", "platform_role", "include_total",
    "limit", "cursor", "offset", "sort", "direction", "active_since", "inactive_since", "never_active", "pending",
}


def _key_status(key) -> Optional[ServiceAccountKeyStatus]:
    return key_status(key)


def _user_capabilities(caller: Principal, user: User) -> UserCapabilities:
    caller_is_admin = caller.is_platform_admin
    is_self = caller.is_user(user)
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
               with_capabilities: bool = False, sign_in: Optional[list[str]] = None) -> UserRead:
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
            "sign_in_methods": sign_in,
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
    Exact unique_id, username, and email filters search every user and return the same
    authorized representation as GET /users/{user_ref}; unique_id and username are
    repeatable, at most 100 values each. Any other query parameter is ignored
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

    if params.pending:
        require_platform_admin(auth_res)
    search_cmd = select(User).where(User.pending_at != None if params.pending else User.pending_at == None)  # noqa: E711
    if params.unique_id is not None:
        search_cmd = search_cmd.where(or_(
            User.unique_id.in_(params.unique_id),
            User.id.in_(select(UserIdentity.user_id).where(UserIdentity.issuer == default_orcid_issuer(), UserIdentity.subject.in_(params.unique_id))),
        ))
    if params.username is not None:
        search_cmd = search_cmd.where(func.lower(User.username).in_([username.lower() for username in params.username]))
    if params.email is not None:
        search_cmd = search_cmd.where(func.lower(User.email) == params.email.lower())

    if params.first_name is not None:
        if params.permissive:
            search_cmd = search_cmd.where(User.first_name.icontains(params.first_name, autoescape=True))
        else:
            search_cmd = search_cmd.where(User.first_name == params.first_name)
    if params.last_name is not None:
        if params.permissive:
            search_cmd = search_cmd.where(User.last_name.icontains(params.last_name, autoescape=True))
        else:
            search_cmd = search_cmd.where(User.last_name == params.last_name)

    if params.q is not None:
        fields = params.search_fields or [
            field for field in UserSearchField
            if is_admin or field is not UserSearchField.EMAIL
        ]
        search_cmd = search_cmd.where(or_(*(
            _USER_SEARCH_COLUMNS[field].icontains(params.q, autoescape=True) for field in dict.fromkeys(fields)
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

    with Session(get_engine()) as session:
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
        if is_admin:
            key_status_by_user = {user_id: _key_status(key) for user_id, key in newest_keys(session, [user.id for user in rows]).items()}
        labels = sign_in_labels(session, [user.id for user in rows]) if is_admin and params.pending else {}
        items = [
            _user_read(auth_res, user, key_status=key_status_by_user.get(user.id), with_capabilities=True, sign_in=labels.get(user.id))
            if is_admin or auth_res.is_user(user)
            else _user_public_read(auth_res, user, with_capabilities=True)
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


@router.post("/users/resolve", response_model=Dict[str, UserPublicRead], deprecated=True)
def resolve_users(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    response: Response,
    orcids: Optional[List[str]] = Body(default=None, max_length=1000),
    usernames: Optional[List[str]] = Body(default=None, max_length=1000),
    emails: Optional[List[str]] = Body(default=None, max_length=1000),
):
    '''
    Batch-resolve users by any mix of ORCIDs, usernames, or emails.
    `orcids` is matched against unique_id, so it also accepts service-account
    MFIDs; the response is keyed by unique_id. No email included.
    Any authenticated user can call this. Admins needing email should use GET /users/{user_ref}.
    Deprecated: use GET /users with repeatable unique_id or username filters, or an exact email filter.
    '''
    response.headers["Deprecation"] = USER_RESOLVE_DEPRECATION
    if not orcids and not usernames and not emails:
        return {}

    with Session(get_engine()) as session:
        results: Dict[str, UserPublicRead] = {}

        if orcids:
            linked = select(UserIdentity.user_id).where(UserIdentity.issuer == default_orcid_issuer(), UserIdentity.subject.in_(orcids))
            for user in session.exec(select(User).where(or_(User.unique_id.in_(orcids), User.id.in_(linked)), User.pending_at == None)).all():  # noqa: E711
                results[user.unique_id] = UserPublicRead.model_validate(user)

        if usernames:
            normalized_usernames = [u.lower() for u in usernames]
            for user in session.exec(select(User).where(User.username.in_(normalized_usernames), User.pending_at == None)).all():  # noqa: E711
                if user.unique_id not in results:
                    results[user.unique_id] = UserPublicRead.model_validate(user)

        if emails:
            normalized_emails = [e.lower() for e in emails]
            for user in session.exec(select(User).where(func.lower(User.email).in_(normalized_emails), User.pending_at == None)).all():  # noqa: E711
                if user.unique_id not in results:
                    results[user.unique_id] = UserPublicRead.model_validate(user)

        return results


@router.get("/users/search", response_model=PaginatedResponse[UserPublicRead])
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
    Search users by username, first name, or last name, ordered by match quality.
    Returns public profiles only (no email) in the shared paginated envelope.
    Minimum query length: 3 characters. Maximum results: 50.
    '''
    normalized_q, tokens = normalize_search_query(q)
    with Session(get_engine()) as session:
        configure_trigram_search(session, get_trgm_search_threshold())
        fields = (User.first_name, User.last_name, User.username)
        query = apply_trigram_search(
            select(User).where(User.pending_at == None),  # noqa: E711
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
    return PaginatedResponse(limit=limit, items=results)


@router.get("/users/by-username/{username}", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def get_user_by_username(username: str):
    """Removed: use GET /users/{user_ref}, which takes a username."""
    removed("This route was removed. GET /users/{user_ref} takes a username, an ORCID or a service-account MFID.")


def _canonical_user_id(user_ref: str) -> str:
    """Map a username, email or linked ORCID path segment to the user's unique_id. MFIDs and
    unresolvable values pass through so the route's own exact lookup and error handling apply
    unchanged."""
    with Session(get_engine()) as session:
        if is_valid_mfid(user_ref):
            unique_id = user_ref
        else:
            try:
                unique_id = resolve_user_by_identifier(session, user_ref).unique_id
            except HTTPException:
                unique_id = user_ref
        if session.exec(select(User.id).where(User.unique_id == unique_id, User.pending_at != None)).first() is not None:  # noqa: E711
            raise HTTPException(status_code=404, detail="User not found")
        return unique_id


@router.get("/users/{user_ref}", response_model=Union[UserRead, UserPublicRead])
def read_user(user_ref: UserRef, response: Response,
              auth_res: Annotated[Principal, Depends(authorize_user)]):
    with Session(get_engine()) as session:
        is_admin = auth_res.is_platform_admin
        user = person_by_reference(session, user_ref, include_pending=True) if is_admin else None
        user = user or resolve_user_by_identifier(session, user_ref)
        if user_ref != user.unique_id:
            response.headers["Content-Location"] = f"/users/{user.unique_id}"
        if auth_res.is_user(user) or is_admin:
            key_status = _admin_key_status(session, auth_res, user)
            return _user_read(auth_res, user, key_status=key_status, with_capabilities=True, sign_in=sign_in_labels(session, [user.id]).get(user.id))
        return _user_public_read(auth_res, user, with_capabilities=True)


def _admin_key_status(session, caller: Principal, user: User) -> Optional[ServiceAccountKeyStatus]:
    if not caller.is_platform_admin:
        return None
    return _key_status(newest_key(session, user.id))


@router.post("/users/{user_ref}/apikey", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def get_or_provision_user_apikey(user_ref: str):
    """Removed: people create their own keys with POST /account/apikeys."""
    removed("Reading, creating or regenerating someone's API key was removed. People create their own keys with POST /account/apikeys; administrators list and revoke them with GET and DELETE /users/{user_ref}/apikeys.")


@router.post("/users/{user_ref}/apikey/regenerate", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def regenerate_user_apikey(user_ref: str):
    """Removed: administrators revoke keys with DELETE /users/{user_ref}/apikeys/{key_id}."""
    removed("Reading, creating or regenerating someone's API key was removed. People create their own keys with POST /account/apikeys; administrators list and revoke them with GET and DELETE /users/{user_ref}/apikeys.")


@router.get("/users/{user_ref}/apikey/verify", response_model=ApiKeyVerify, deprecated=True)
def verify_user_apikey(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)], response: Response):
    '''Returns validity and expiry info for the given user's API key. Admin only.

    Deprecated: administrators get the same status as `api_key_status` from GET /users/{user_ref}.
    '''
    user_id = _canonical_user_id(user_ref)
    require_platform_admin(auth_res)
    response.headers["Deprecation"] = API_KEY_VERIFY_DEPRECATION

    with Session(get_engine()) as session:
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
    with Session(get_engine()) as session:
        if session.exec(select(User).where(User.unique_id == unique_id)).one_or_none() is not None:
            raise HTTPException(status_code=409, detail=f"unique_id '{unique_id}' already exists")
        if session.exec(select(AccessGroup).where(AccessGroup.group_name == unique_id)).one_or_none() is not None:
            raise HTTPException(status_code=409, detail=f"Identity group '{unique_id}' already exists")
        if is_orcid(unique_id) and find_identity(session, default_orcid_issuer(), unique_id) is not None:
            raise HTTPException(status_code=409, detail=IDENTITY_LINKED_ELSEWHERE)
        assert_profile_fields_available(session, username=user_info.username or None, email=user_info.email)

    explicit_project_ids = set(project_ids or [])
    discovered_project_ids = set(find_mf_user_proposals(unique_id)) if is_orcid(unique_id) else set()

    with Session(get_engine()) as session:
        projects_by_mfid = {}
        missing_explicit = []
        for ref in sorted(explicit_project_ids):
            project = resolve_project_reference(ref, session)
            if project is None:
                missing_explicit.append(ref)
                continue
            assert_not_pending_deletion(session, project.unique_id)
            projects_by_mfid[project.unique_id] = project
        if missing_explicit:
            raise HTTPException(
                status_code=404,
                detail=f"Projects not found: {', '.join(missing_explicit)}",
            )
        discovered_projects, _ = usable_discovered_projects(session, sorted(discovered_project_ids - explicit_project_ids))
        projects_by_mfid.update({project.unique_id: project for project in discovered_projects})
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

        email = user_info.email.lower().strip() if user_info.email is not None else None
        assert_profile_fields_available(session, username=user_info.username or None, email=user_info.email)
        try:
            user = provision_user(
                session, unique_id=unique_id, first_name=user_info.first_name, last_name=user_info.last_name,
                email=email, username=user_info.username,
                project_groups=[groups_by_name[project.unique_id] for project in projects],
            )
            session.commit()
        except IdentityTaken:
            raise HTTPException(status_code=409, detail=IDENTITY_LINKED_ELSEWHERE) from None
        except IntegrityError as error:
            conflict = profile_conflict(error)
            if conflict is None and "unique constraint" in str(error.orig).lower():
                conflict = HTTPException(status_code=409, detail="Another request created this user at the same time; nothing created")
            if conflict is None:
                raise
            raise conflict from None
        session.refresh(user)

    return _user_read(auth_res, user, with_capabilities=True)


@router.patch("/users/{user_ref}", response_model=UserRead, responses={409: {"description": "Username or email already held by another user: `{\"detail\": {\"code\": \"already_in_use\", \"fields\": [\"username\", \"email\"], \"message\": ...}}`, listing every conflicting field"}})
def update_user(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)], user_update: UserUpdate):
    '''Partially update a user record. Only fields provided in the request body are modified. Changing another human administrator needs a Hub session with a recent sign-in.'''
    user_id = _canonical_user_id(user_ref)
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        require_self_or_platform_admin(auth_res, user)

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not auth_res.is_user(user):
            require_human_admin_safeguard(auth_res, user)
        update_data = user_update.model_dump(exclude_unset=True)
        if not auth_res.is_platform_admin:
            assert_self_chosen_username_allowed(update_data.get("username"), user.username)
        assert_profile_fields_available(session, username=update_data.get("username"), email=update_data.get("email"), exclude_orcid=user_id)
        assert_username_kept_for_password(session, user, update_data)
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

    with Session(get_engine()) as session:
        user = resolve_user_by_identifier(session, user_ref)
        if user.is_service_account:
            raise HTTPException(
                status_code=409,
                detail=(f"{user.unique_id} is a service account. "
                        f"Use PATCH /service_accounts/{user.unique_id} instead."),
            )
        if auth_res.is_user(user):
            raise HTTPException(status_code=403, detail="Callers cannot change their own platform role")
        require_human_admin_safeguard(auth_res, user, makes_admin=body.platform_role == PlatformRole.ADMIN.value)

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
        record_event(session, AuthEventType.ROLE_CHANGED, user=user, actor=caller_row(session, auth_res))
        session.commit()
        forget_cached_keys(session, user.id)
        session.refresh(user)
        key_status = _admin_key_status(session, auth_res, user)
        return _user_read(auth_res, user, key_status=key_status, with_capabilities=True)


def _account_to_switch(session: Session, user_ref: str, auth_res: Principal) -> User:
    require_platform_admin(auth_res)
    if auth_res.is_service_account:
        raise HTTPException(status_code=403, detail="Service accounts cannot switch accounts off or on")
    user = resolve_user_by_identifier(session, user_ref)
    if auth_res.is_user(user):
        raise HTTPException(status_code=403, detail="Callers cannot switch their own account off or on")
    require_human_admin_safeguard(auth_res, user)
    require_recent_authentication(auth_res)
    return user


def _pending_account(session: Session, user_ref: str, auth_res: Principal) -> User:
    require_platform_admin(auth_res)
    require_recent_authentication(auth_res)
    user = person_by_reference(session, user_ref, include_pending=True)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


_PENDING_RESPONSES = {401: {"description": "A session needs a recent sign-in (`insufficient_user_authentication`)"}, 403: {"description": "Not a platform administrator, or `browser_sign_in_required` for a session approved with a device code"}, 404: {"description": "No such account"}, 409: {"description": "`not_pending`: the account is not waiting for approval"}}


@router.post("/users/{user_ref}/approve", response_model=UserRead, responses=_PENDING_RESPONSES)
def approve_user(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Let an account that waits for approval in. Platform administrators only."""
    with Session(get_engine()) as session:
        user = _pending_account(session, user_ref, auth_res)
        try:
            user = approve_pending_user(session, user, actor=caller_row(session, auth_res))
        except NotPending:
            raise HTTPException(status_code=409, detail={"code": "not_pending", "message": "This account is not waiting for approval"})
        session.commit()
        session.refresh(user)
        return _user_read(auth_res, user, with_capabilities=True)


@router.post("/users/{user_ref}/reject", status_code=204, responses={**_PENDING_RESPONSES, 409: {"description": "`not_pending`, or `still_referenced` when something besides its own group and logins points at the account"}})
def reject_user(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Delete an account that waits for approval, freeing its username and login. Active accounts
    are never deleted. Platform administrators only."""
    with Session(get_engine()) as session:
        user = _pending_account(session, user_ref, auth_res)
        try:
            reject_pending_user(session, user, actor=caller_row(session, auth_res))
        except NotPending:
            raise HTTPException(status_code=409, detail={"code": "not_pending", "message": "This account is not waiting for approval"})
        except StillReferenced as referenced:
            raise HTTPException(status_code=409, detail={"code": "still_referenced", "message": "; ".join(referenced.reasons)})
        session.commit()


@router.post("/users/{user_ref}/disable", response_model=UserRead, responses={401: {"description": "A Hub session needs a recent sign-in to change a human administrator"}, 409: {"description": "This is the last active human platform administrator"}})
def disable_user(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Switch an account off: every sign-in, API key and Hub session stops working. Other server
    workers honor it within the API key cache lifetime. Platform administrators only."""
    with Session(get_engine()) as session:
        user = _account_to_switch(session, user_ref, auth_res)
        if user.disabled_at is None:
            if user.platform_role == PlatformRole.ADMIN.value and not user.is_service_account:
                # Lock every human admin row, so two administrators disabling each other cannot both succeed.
                active_admins = session.exec(
                    select(User.id).where(User.platform_role == PlatformRole.ADMIN.value, User.is_service_account == False, User.disabled_at == None).order_by(User.id).with_for_update()  # noqa: E711, E712
                ).all()
                if len([admin_id for admin_id in active_admins if admin_id != user.id]) == 0:
                    raise HTTPException(status_code=409, detail="Cannot disable the last active human platform administrator")
            user.disabled_at = datetime.now(timezone.utc)
            session.add(user)
            revoke_all_tokens(session, user.id)
            void_links(session, user)
            record_event(session, AuthEventType.DISABLED, user=user, actor=caller_row(session, auth_res))
            session.commit()
            forget_cached_keys(session, user.id)
        session.refresh(user)
        return _user_read(auth_res, user, key_status=_admin_key_status(session, auth_res, user), with_capabilities=True)


@router.post("/users/{user_ref}/enable", response_model=UserRead, responses={401: {"description": "A Hub session needs a recent sign-in to change a human administrator"}})
def enable_user(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Switch an account back on. Hub sessions ended by disabling stay ended. Platform administrators only."""
    with Session(get_engine()) as session:
        user = _account_to_switch(session, user_ref, auth_res)
        if user.disabled_at is not None:
            user.disabled_at = None
            session.add(user)
            record_event(session, AuthEventType.ENABLED, user=user, actor=caller_row(session, auth_res))
            session.commit()
        session.refresh(user)
        return _user_read(auth_res, user, key_status=_admin_key_status(session, auth_res, user), with_capabilities=True)


@router.delete("/users/{user_ref}/apikey", status_code=204)
def delete_user_apikey(
    user_ref: UserRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    '''Revoke every API key held by the user. Platform administrators only; another human administrator's keys need a Hub session with a recent sign-in. 204 on success.'''
    user_id = _canonical_user_id(user_ref)
    require_platform_admin(auth_res)

    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        if not auth_res.is_user(user):
            require_human_admin_safeguard(auth_res, user)

        replace_plain_keys(session, user.id)
        revoke_user_keys(session, user.id)
        session.commit()
        forget_cached_keys(session, user.id)
    return Response(status_code=204)


@router.get("/users/{user_ref}/datasets", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def read_user_datasets(user_ref: str):
    """Removed: use GET /datasets?accessible_to_user={user_ref}."""
    removed("This route was removed. List the datasets a person can read with GET /datasets?accessible_to_user={user_ref}.")


@router.get("/users/{user_ref}/samples", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def read_user_samples(user_ref: str):
    """Removed: use GET /samples?accessible_to_user={user_ref}."""
    removed("This route was removed. List the samples a person can read with GET /samples?accessible_to_user={user_ref}.")


@router.get("/users/{user_ref}/datasets/{dataset_mfid}", response_model=EffectiveResourceAccessRead)
def read_user_access_to_dataset(dataset_mfid: DatasetMfid, user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    '''Returns the user's effective role on one dataset. Limited to self and platform administrators.'''
    with Session(get_engine()) as session:
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
    with Session(get_engine()) as session:
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
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        require_self_or_platform_admin(auth_res, user)

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
    with Session(get_engine()) as session:
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
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.unique_id == user_id)).one_or_none()
        if not auth_res.is_user(user) and not has_global_permission(auth_res, Permission.READ):
            raise HTTPException(status_code=403, detail="Not authorized")

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
        leads = project_leads(session, projects)
        items = [
            ProjectWithLead(
                **project_fields(proj, leads.get(proj.lead_user_id)),
                lead=leads.get(proj.lead_user_id),
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
