from datetime import timedelta, datetime, timezone
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy import delete, exists, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from app.api.removed import REMOVED_RESPONSE, removed
from app.api.dependencies import get_engine
from app.general_utils import get_utc_datetime, if_match_satisfied
from app.tables import Dataset, DeletionRequest, Instrument, Project, Resource, Sample, User, UserPreferences, UserRecentResource
from app.models.preferences import MAX_DOCUMENT_BYTES, PREFERENCES_SCHEMA_VERSION, PreferencesDocument, PreferencesRead, RecentResourceRead, RecentResourceRecord, serialized_size
from app.api.routes.join_requests import join_request_filters, list_join_requests_page
from app.models.join_requests import JoinRequestRead, JoinRequestScope, JoinRequestSort, JoinRequestStatus
from app.models.query_params import SortDirection
from app.models.pagination import PaginatedResponse
from app.models.response_schemas import AccountInfo, ApiKeyVerify, ProjectSyncResult
from app.models.users import (
    AccountAuthorizationRead,
    AccountCapabilities,
    AccountProfileRead,
    AccountProfileUpdate,
    UserRead,
)
from app.utils.auth_utils import caller_row, authorize_user, security, permission_filter
from app.utils.permissions import (
    Permission,
    PlatformRole,
    Principal,
    SESSION,
    has_global_permission,
    resolve_authorization_context,
)
from app.api.errors import feature_disabled_response
from app.config import require_feature
from app.auth.linking import sign_in_labels
from app.auth.api_keys import find_api_key, key_status, newest_key
from app.auth.identity import linked_orcid
from app.services.proposals import ProposalServiceUnavailable, assign_discovered_projects, lookup_proposals
from app.utils.user_utils import assert_profile_fields_available, assert_username_kept_for_password, assert_self_chosen_username_allowed, commit_user

router = APIRouter(tags=["account"])


def _self_user_read(user: User) -> UserRead:
    role = None
    if user.platform_role is not None:
        try:
            role = PlatformRole(user.platform_role)
        except ValueError:
            role = None
    return UserRead.model_validate(user, update={"platform_role": role})


@router.get("/account", response_model=AccountInfo)
def get_account_info(auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Return auth context for the authenticated caller: ORCID, access groups, and profile."""
    user_unique_id, ag_list = auth_res.user_id, auth_res.groups
    ag_ids = [ag.id for ag in ag_list]
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        user_info = _self_user_read(user) if user else None
    return AccountInfo(user_unique_id=user_unique_id, access_group_ids=ag_ids, user_info=user_info)


def _account_capabilities(principal: Principal) -> AccountCapabilities:
    return AccountCapabilities(
        can_manage_service_accounts=principal.is_platform_admin,
        can_manage_users=principal.is_platform_admin,
        can_create_project=True,
        can_register_instrument=not principal.is_service_account,
        can_create_sample=True,
        can_create_dataset=True,
        can_create_for_others=has_global_permission(principal, Permission.CREATE),
    )


@router.get("/account/profile", response_model=AccountProfileRead)
def get_account_profile(
    request: Request,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    """Return the authenticated caller's profile and presentation capabilities."""
    user_unique_id = auth_res.user_id
    with Session(get_engine()) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        context = getattr(request.state, "authorization_context", None)
        if context is None:
            context = resolve_authorization_context(auth_res, None)
        return AccountProfileRead.model_validate(
            user,
            update={
                "authorization": AccountAuthorizationRead(
                    platform_role=context.actor.platform_role,
                    effective_privilege_mode=context.privilege_mode,
                    can_elevate=context.can_elevate,
                ),
                "capabilities": _account_capabilities(auth_res),
                "sign_in_methods": sign_in_labels(session, [user.id]).get(user.id),
            },
        )


@router.patch("/account/profile", response_model=UserRead, responses={409: {"description": "Username or email already held by another user: `{\"detail\": {\"code\": \"already_in_use\", \"fields\": [\"username\", \"email\"], \"message\": ...}}`, listing every conflicting field"}})
def update_account_profile(auth_res: Annotated[Principal, Depends(authorize_user)], user_update: AccountProfileUpdate):
    """Partially update the authenticated caller's own profile."""
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        update_data = user_update.model_dump(exclude_unset=True)
        if not auth_res.is_platform_admin:
            assert_self_chosen_username_allowed(update_data.get("username"), user.username)
        assert_profile_fields_available(session, username=update_data.get("username"), email=update_data.get("email"), exclude_orcid=user.unique_id)
        assert_username_kept_for_password(session, user, update_data)
        for key, value in update_data.items():
            setattr(user, key, value)
        session.add(user)
        commit_user(session)
        session.refresh(user)
        return _self_user_read(user)


@router.post("/account/sync-projects", response_model=ProjectSyncResult, responses={**feature_disabled_response("proposal_sync"), 502: {"description": "The proposal service could not be reached"}})
def sync_account_projects(auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Add project memberships discovered from the MF proposal database."""
    require_feature("proposal_sync")
    with Session(get_engine()) as session:
        orcid = linked_orcid(session, caller_row(session, auth_res))
    try:
        proposal_ids = lookup_proposals(orcid) if orcid else []
    except ProposalServiceUnavailable:
        raise HTTPException(status_code=502, detail="The proposal service could not be reached; try again.") from None
    with Session(get_engine()) as session:
        result = assign_discovered_projects(session, caller_row(session, auth_res), proposal_ids)
        session.commit()
    return ProjectSyncResult(orcid=orcid, **result)


@router.get("/account/join_requests", response_model=PaginatedResponse[JoinRequestRead], deprecated=True)
def get_account_join_requests(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    status: Optional[JoinRequestStatus] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Deprecated: use GET /join_requests?scope=submitted."""
    filters = join_request_filters(scope=JoinRequestScope.SUBMITTED, status=[status] if status else None, project_mfid=None,
                                   group_name=None, requester_id=None, reviewer_id=None, requested_after=None,
                                   requested_before=None, reviewed_after=None, reviewed_before=None, q=None)
    with Session(get_engine()) as session:
        return list_join_requests_page(session, auth_res, filters, JoinRequestSort.REQUEST_TIME, SortDirection.DESC, limit, None, offset)


@router.get("/account/apikey", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def get_account_apikey():
    """Removed: API keys are stored hashed and cannot be read back. Use POST /account/apikeys."""
    removed("Reading or regenerating a single API key was removed. Create a named key with POST /account/apikeys (it is shown once), list them with GET /account/apikeys and revoke one with DELETE /account/apikeys/{key_id}.")


@router.post("/account/apikey/regenerate", deprecated=True, status_code=410, responses=REMOVED_RESPONSE)
def regenerate_account_apikey():
    """Removed: create keys with POST /account/apikeys and revoke them with DELETE /account/apikeys/{key_id}."""
    removed("Reading or regenerating a single API key was removed. Create a named key with POST /account/apikeys (it is shown once), list them with GET /account/apikeys and revoke one with DELETE /account/apikeys/{key_id}.")


@router.get("/account/verify", response_model=ApiKeyVerify, responses={404: {"description": "A Hub session's account has no API key"}})
def verify_account_apikey(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)],
):
    """
    Report an API key's validity and expiry. With an API key, describes that key (expired keys
    are rejected with 401 before this runs). With a Hub session, describes the account's newest key,
    with valid false once it has expired.
    """
    with Session(get_engine()) as session:
        if auth_res.credential == SESSION:
            key = newest_key(session, auth_res.internal_user_id) if auth_res.internal_user_id is not None else None
        else:
            key = find_api_key(session, credentials.credentials)
        if key is None:
            raise HTTPException(status_code=404, detail="No API key found")
        status = key_status(key)
        return ApiKeyVerify(valid=status.valid, created_at=status.created_at, expires_at=status.expires_at, last_used_at=status.last_used_at)


def _merge_patch(target, patch):
    if not isinstance(patch, dict):
        return patch
    result = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = _merge_patch(result.get(key), value)
    return result


def _preferences_etag(revision: int) -> str:
    return f'"p{revision}"'


def _preferences_read(row: Optional[UserPreferences], response: Response) -> PreferencesRead:
    revision = row.revision if row else 0
    response.headers["ETag"] = _preferences_etag(revision)
    if row is None:
        return PreferencesRead(schema_version=PREFERENCES_SCHEMA_VERSION, revision=0, preferences={})
    return PreferencesRead(schema_version=row.schema_version, revision=row.revision, updated_at=row.updated_at, preferences=row.preferences)


def _caller_id(session: Session, principal: Principal) -> int:
    user = caller_row(session, principal)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user.id


@router.get("/account/preferences", response_model=PreferencesRead, responses={200: {"headers": {"ETag": {"description": "Current preferences revision; send it as `If-Match` on PATCH", "schema": {"type": "string"}}}}})
def get_account_preferences(auth_res: Annotated[Principal, Depends(authorize_user)], response: Response):
    """Return the caller's preferences. A caller who never saved any gets an empty document at revision 0."""
    with Session(get_engine()) as session:
        row = session.get(UserPreferences, _caller_id(session, auth_res))
        return _preferences_read(row, response)


@router.patch("/account/preferences", response_model=PreferencesRead, responses={200: {"headers": {"ETag": {"description": "New preferences revision", "schema": {"type": "string"}}}}, 412: {"description": "If-Match does not match the current revision: `{\"detail\": {\"code\": \"etag_mismatch\", \"etag\": ..., \"message\": ...}}`"}, 422: {"description": "The patch is not an object, or the merged document has an unknown key, an invalid value, or exceeds a size limit"}})
def update_account_preferences(auth_res: Annotated[Principal, Depends(authorize_user)], response: Response, patch: Annotated[Any, Body()], if_match: Annotated[Optional[str], Header()] = None):
    """Merge a JSON Merge Patch (RFC 7396) into the caller's preferences: `null` removes a key. Send `If-Match` with the ETag from GET to refuse the change when another write happened first; `If-Match: *` matches any revision."""
    if not isinstance(patch, dict):
        raise RequestValidationError([{"type": "dict_type", "loc": ("body",), "msg": "Preferences patch must be a JSON object", "input": patch}])
    for attempt in range(2):
        with Session(get_engine()) as session:
            user_id = _caller_id(session, auth_res)
            row = session.exec(select(UserPreferences).where(UserPreferences.user_id == user_id).with_for_update()).one_or_none()
            current = _preferences_etag(row.revision if row else 0)
            if not if_match_satisfied(if_match, current):
                raise HTTPException(status_code=412, detail={"code": "etag_mismatch", "etag": current, "message": "Preferences changed since the given ETag"})
            merged = _merge_patch(row.preferences if row else {}, patch)
            try:
                PreferencesDocument.model_validate(merged)
            except ValidationError as error:
                raise RequestValidationError(error.errors(include_url=False, include_context=False)) from None
            if serialized_size(merged) > MAX_DOCUMENT_BYTES:
                raise RequestValidationError([{"type": "too_long", "loc": ("body",), "msg": f"Preferences exceed {MAX_DOCUMENT_BYTES} bytes", "input": None}])
            if row is None:
                row = UserPreferences(user_id=user_id, preferences=merged, schema_version=PREFERENCES_SCHEMA_VERSION, revision=1, updated_at=get_utc_datetime())
            else:
                row.preferences = merged
                row.revision += 1
                row.updated_at = get_utc_datetime()
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                if attempt:
                    raise
                session.rollback()
                continue
            session.refresh(row)
            return _preferences_read(row, response)


RECENT_LIMIT = 50
RECENT_RETENTION = timedelta(days=90)
RECENT_THROTTLE = timedelta(seconds=60)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _readable_resources(principal: Principal):
    return select(Resource.mfid).where(
        permission_filter(principal, Resource, Permission.READ, resource_id_column=Resource.mfid),
        ~exists().where(DeletionRequest.resource_id == Resource.mfid, DeletionRequest.status.in_(["pending", "approved"])),
    )


def _records_recent_resources(session: Session, user_id: int) -> bool:
    row = session.get(UserPreferences, user_id)
    return row is None or row.preferences.get("record_recent_resources") is not False


@router.post("/account/recent-resources", status_code=204, responses={404: {"description": "The resource does not exist or the caller cannot read it"}})
def record_recent_resource(auth_res: Annotated[Principal, Depends(authorize_user)], body: RecentResourceRecord):
    """Record that the caller viewed a resource. Repeat views within a minute are not rewritten; the newest 50 entries are kept. Stores nothing when the caller's `record_recent_resources` preference is false."""
    with Session(get_engine()) as session:
        user_id = _caller_id(session, auth_res)
        if session.exec(_readable_resources(auth_res).where(Resource.mfid == body.resource_mfid)).first() is None:
            raise HTTPException(status_code=404, detail="Resource not found")
        if not _records_recent_resources(session, user_id):
            return Response(status_code=204)
        now = get_utc_datetime()
        row = session.get(UserRecentResource, (user_id, body.resource_mfid))
        if row is None:
            session.add(UserRecentResource(user_id=user_id, resource_mfid=body.resource_mfid, viewed_at=now))
        elif _aware(row.viewed_at) <= now - RECENT_THROTTLE:
            row.viewed_at = now
            session.add(row)
        else:
            return Response(status_code=204)
        session.flush()
        keep = select(UserRecentResource.resource_mfid).where(UserRecentResource.user_id == user_id).order_by(UserRecentResource.viewed_at.desc()).limit(RECENT_LIMIT)
        session.execute(delete(UserRecentResource).where(
            UserRecentResource.user_id == user_id,
            or_(UserRecentResource.resource_mfid.not_in(keep), UserRecentResource.viewed_at < now - RECENT_RETENTION),
        ))
        session.commit()
    return Response(status_code=204)


@router.get("/account/recent-resources", response_model=list[RecentResourceRead])
def list_recent_resources(auth_res: Annotated[Principal, Depends(authorize_user)], limit: Annotated[int, Query(ge=1, le=RECENT_LIMIT)] = 10):
    """Return the caller's recently viewed resources, newest first, with `identifier` set to the instrument or project slug (null for datasets and samples), limited to resources the caller can still read and that are not pending deletion. Entries older than 90 days are omitted."""
    name = func.coalesce(Dataset.dataset_name, Sample.sample_name, Project.title, Project.project_id, Instrument.instrument_name)
    record_project = aliased(Project)
    project = func.coalesce(record_project.project_id, Project.project_id)
    identifier = func.coalesce(Instrument.instrument_id, Project.project_id)
    with Session(get_engine()) as session:
        user_id = _caller_id(session, auth_res)
        statement = (
            select(UserRecentResource.resource_mfid, Resource.resource_type, name, project, identifier, UserRecentResource.viewed_at)
            .join(Resource, Resource.mfid == UserRecentResource.resource_mfid)
            .outerjoin(Dataset, Dataset.unique_id == Resource.mfid)
            .outerjoin(Sample, Sample.unique_id == Resource.mfid)
            .outerjoin(record_project, record_project.unique_id == func.coalesce(Dataset.project_mfid, Sample.project_mfid))
            .outerjoin(Project, Project.unique_id == Resource.mfid)
            .outerjoin(Instrument, Instrument.unique_id == Resource.mfid)
            .where(
                UserRecentResource.user_id == user_id,
                UserRecentResource.viewed_at >= get_utc_datetime() - RECENT_RETENTION,
                Resource.resource_type.in_(["dataset", "sample", "project", "instrument"]),
                UserRecentResource.resource_mfid.in_(_readable_resources(auth_res)),
            )
            .order_by(UserRecentResource.viewed_at.desc())
            .limit(limit)
        )
        return [
            RecentResourceRead(resource_mfid=mfid, resource_type=kind, name=label, project_id=project_id, identifier=identifier, viewed_at=_aware(viewed_at))
            for mfid, kind, label, project_id, identifier, viewed_at in session.exec(statement).all()
        ]


@router.delete("/account/recent-resources", status_code=204)
def clear_recent_resources(auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Clear the caller's recently viewed resources."""
    with Session(get_engine()) as session:
        session.execute(delete(UserRecentResource).where(UserRecentResource.user_id == _caller_id(session, auth_res)))
        session.commit()
    return Response(status_code=204)


@router.delete("/account/recent-resources/{resource_mfid}", status_code=204)
def remove_recent_resource(auth_res: Annotated[Principal, Depends(authorize_user)], resource_mfid: str):
    """Remove one resource from the caller's recently viewed resources. Succeeds when it is not in the list."""
    with Session(get_engine()) as session:
        session.execute(delete(UserRecentResource).where(UserRecentResource.user_id == _caller_id(session, auth_res), UserRecentResource.resource_mfid == resource_mfid))
        session.commit()
    return Response(status_code=204)
