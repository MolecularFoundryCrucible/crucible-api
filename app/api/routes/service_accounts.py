from datetime import date, datetime, timedelta, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import ServiceAccountRef
from app.models.core_models import AccessGroup, ApiToken, Resource, User
from app.models.pagination import PaginatedResponse
from app.models.users import (
    ServiceAccountAdminDetail,
    ServiceAccountAdminRead,
    ServiceAccountCreate,
    ServiceAccountKeyStatus,
    ServiceAccountPlatformRoleUpdate,
    ServiceAccountRead,
)
from app.utils.auth_utils import authorize_user, find_or_add_api_access, require_platform_admin, require_recent_authentication
from app.utils.permissions import AccessGroupType, PlatformRole, Principal, Role
from app.utils.user_utils import assert_username_available, resolve_service_account_reference, sql_add_user_to_access_group

router = APIRouter(tags=["service_accounts"])
engine = get_engine()


def _service_account_admin_read(user: User) -> ServiceAccountAdminRead:
    return ServiceAccountAdminRead.model_validate(user)


def _key_status(token: Optional[ApiToken]) -> Optional[ServiceAccountKeyStatus]:
    if token is None:
        return None
    created_at = token.token_created
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if isinstance(created_at, date) and not isinstance(created_at, datetime):
        created_at = datetime.combine(created_at, datetime.min.time(), tzinfo=timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    expires_at = created_at + timedelta(days=365)
    return ServiceAccountKeyStatus(
        valid=expires_at >= datetime.now(timezone.utc),
        created_at=created_at.date().isoformat(),
        expires_at=expires_at.date().isoformat(),
    )


def _service_account_admin_detail(
    session: Session,
    user: User,
) -> ServiceAccountAdminDetail:
    token = session.exec(
        select(ApiToken)
        .where(ApiToken.user_unique_id == user.unique_id)
        .order_by(ApiToken.token_created.desc())
    ).first()
    return ServiceAccountAdminDetail.model_validate(
        user,
        update={"api_key_status": _key_status(token)},
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

    with Session(engine) as session:
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
    with Session(engine) as session:
        user = resolve_service_account_reference(session, service_account_ref)
        return _service_account_admin_detail(session, user)


@router.patch("/service_accounts/{service_account_ref}", response_model=ServiceAccountAdminDetail)
def update_service_account_platform_role(
    service_account_ref: ServiceAccountRef,
    body: ServiceAccountPlatformRoleUpdate,
    auth_res: Annotated[Principal, Depends(authorize_user)],
):
    """Update a service account's platform role."""
    require_platform_admin(auth_res)
    if auth_res.is_service_account:
        raise HTTPException(
            status_code=403,
            detail="Service accounts cannot manage platform roles",
        )

    with Session(engine) as session:
        user = resolve_service_account_reference(session, service_account_ref)

        user.platform_role = body.platform_role
        session.add(user)
        session.commit()
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

    with Session(engine) as session:
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
            from app.models.core_models import get_mfid_value
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

    # Generate API key (outside session — find_or_add_api_access opens its own)
    api_key = find_or_add_api_access(unique_id, engine, regenerate=False)
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

    with Session(engine) as session:
        user = resolve_service_account_reference(session, service_account_ref)
        unique_id = user.unique_id
        username = user.username

    api_key = find_or_add_api_access(unique_id, engine, regenerate=True)
    response.headers["Cache-Control"] = "no-store"

    return ServiceAccountRead(
        unique_id=unique_id,
        username=username,
        api_key=api_key,
    )
