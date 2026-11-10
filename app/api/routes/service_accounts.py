from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import ServiceAccountRef
from app.tables import AccessGroup, Resource, User
from app.models.pagination import PaginatedResponse
from app.models.users import (
    ServiceAccountAdminDetail,
    ServiceAccountAdminRead,
    ServiceAccountCreate,
    ServiceAccountKeyStatus,
    ServiceAccountPlatformRoleUpdate,
    ServiceAccountRead,
)
from app.auth.api_keys import key_status, newest_key
from app.auth.events import AuthEventType, record_event
from app.utils.auth_utils import authorize_user, caller_row, find_or_add_api_access, forget_cached_keys, require_platform_admin, require_recent_authentication, require_recent_session
from app.utils.permissions import AccessGroupType, PlatformRole, Principal, Role
from app.utils.user_utils import assert_username_available, resolve_service_account_reference, sql_add_user_to_access_group

router = APIRouter(tags=["service_accounts"])


def _service_account_admin_read(user: User) -> ServiceAccountAdminRead:
    return ServiceAccountAdminRead.model_validate(user)


def _key_status(key) -> Optional[ServiceAccountKeyStatus]:
    return key_status(key)


def _service_account_admin_detail(
    session: Session,
    user: User,
) -> ServiceAccountAdminDetail:
    return ServiceAccountAdminDetail.model_validate(
        user,
        update={"api_key_status": _key_status(newest_key(session, user.id))},
    )


@router.get("/service_accounts", response_model=PaginatedResponse[ServiceAccountAdminRead])
def list_service_accounts(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    q: Optional[str] = Query(default=None, min_length=3, max_length=200),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """List service accounts for platform administration."""
    require_platform_admin(auth_res)
    query = select(User).where(User.is_service_account == True)
    if q is not None:
        normalized = " ".join(q.lower().split())
        if len(normalized) < 3:
            raise HTTPException(
                status_code=422,
                detail="Search query must contain at least 3 non-whitespace characters",
            )
        for token in normalized.split():
            query = query.where(or_(
                User.username.icontains(token, autoescape=True),
                User.first_name.icontains(token, autoescape=True),
                User.last_name.icontains(token, autoescape=True),
            ))

    with Session(get_engine()) as session:
        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        users = session.exec(
            query.order_by(func.lower(User.username), User.unique_id)
            .offset(offset)
            .limit(limit)
        ).all()
    return PaginatedResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=[_service_account_admin_read(user) for user in users],
    )


@router.get("/service_accounts/{service_account_ref}", response_model=ServiceAccountAdminDetail)
def get_service_account(
    service_account_ref: ServiceAccountRef,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    """Return administrator-visible service-account details without a secret."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session:
        user = resolve_service_account_reference(session, service_account_ref)
        return _service_account_admin_detail(session, user)


@router.patch("/service_accounts/{service_account_ref}", response_model=ServiceAccountAdminDetail, responses={401: {"description": "Making a service account an administrator needs a recent sign-in (`insufficient_user_authentication`)"}, 403: {"description": "Not an administrator, or making a service account an administrator with an API key (`hub_session_required`)"}})
def update_service_account_platform_role(
    service_account_ref: ServiceAccountRef,
    body: ServiceAccountPlatformRoleUpdate,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    """Update a service account's platform role. Making one an administrator needs a browser
    session with a recent sign-in, not an API key. The change is recorded as `role_changed`."""
    require_platform_admin(auth_res)
    if auth_res.is_service_account:
        raise HTTPException(
            status_code=403,
            detail="Service accounts cannot manage platform roles",
        )

    with Session(get_engine()) as session:
        user = resolve_service_account_reference(session, service_account_ref)
        if user.platform_role != body.platform_role:
            if body.platform_role == PlatformRole.ADMIN.value:
                require_recent_session(auth_res, "Making a service account an administrator needs a Hub session, not an API key")
            user.platform_role = body.platform_role
            session.add(user)
            record_event(session, AuthEventType.ROLE_CHANGED, user=user, actor=caller_row(session, auth_res))
            session.commit()
            forget_cached_keys(session, user.id)
            session.refresh(user)
        return _service_account_admin_detail(session, user)


@router.post("/service_accounts", response_model=ServiceAccountRead, responses={401: {"description": "Hub sessions need a recent ORCID sign-in (`insufficient_user_authentication`)"}})
def create_service_account(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    body: ServiceAccountCreate,
    response: Response,
):
    '''
    Create a service account and return its API key.
    The API key is only returned once.
    unique_id is optional and must be an MFID; if omitted the server generates one.
    Admin only.
    '''
    require_platform_admin(auth_res)
    require_recent_authentication(auth_res)

    with Session(get_engine()) as session:
        # Username must be unique across all users
        assert_username_available(session, body.username)

        if body.unique_id:
            taken = (
                session.exec(select(User.id).where(User.unique_id == body.unique_id)).first()
                or session.exec(select(Resource.mfid).where(Resource.mfid == body.unique_id)).first()
                or session.exec(select(AccessGroup.id).where(AccessGroup.group_name == body.unique_id)).first()
            )
            if taken:
                raise HTTPException(status_code=409, detail=f"unique_id '{body.unique_id}' already exists.")
            unique_id = body.unique_id
        else:
            from app.tables import get_mfid_value
            unique_id = get_mfid_value()

        user = User(
            unique_id=unique_id,
            username=body.username,
            first_name="",
            last_name="",
            is_service_account=True,
            platform_role=PlatformRole.CONTRIBUTOR.value,
        )
        session.add(user)
        session.flush()  # get user.id

        # Personal access group at owner standing, same as a human user gets
        # at login (see auth.py) -- without it, a resource this SA itself
        # owns would cap its own effective role at viewer via a null standing.
        sql_add_user_to_access_group(
            unique_id,
            unique_id,
            session,
            role=Role.OWNER,
            group_type=AccessGroupType.IDENTITY,
        )
        session.commit()
        user_id = user.id

    # Generate API key (outside session — find_or_add_api_access opens its own)
    api_key = find_or_add_api_access(user_id, get_engine(), regenerate=False)
    response.headers["Cache-Control"] = "no-store"

    return ServiceAccountRead(
        unique_id=unique_id,
        username=body.username,
        api_key=api_key,
    )


@router.post("/service_accounts/{service_account_ref}/rotate_key", response_model=ServiceAccountRead, responses={401: {"description": "Hub sessions need a recent ORCID sign-in (`insufficient_user_authentication`)"}})
def rotate_service_account_key(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    service_account_ref: ServiceAccountRef,
    response: Response,
):
    '''
    Generate a new API key for a service account, invalidating the previous one.
    The new key is only returned once — store it securely.
    Admin only.
    '''
    require_platform_admin(auth_res)
    require_recent_authentication(auth_res)

    with Session(get_engine()) as session:
        user = resolve_service_account_reference(session, service_account_ref)
        user_id, unique_id = user.id, user.unique_id
        username = user.username

    api_key = find_or_add_api_access(user_id, get_engine(), regenerate=True)
    response.headers["Cache-Control"] = "no-store"

    return ServiceAccountRead(
        unique_id=unique_id,
        username=username,
        api_key=api_key,
    )
