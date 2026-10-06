from datetime import timedelta, datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.general_utils import is_orcid
from app.models.core_models import AccessGroupJoinRequest, ApiToken, User
from app.models.join_requests import JoinRequestRead
from app.models.pagination import PaginatedResponse
from app.models.response_schemas import AccountInfo, ApiKeyRead, ApiKeyVerify, ProjectSyncResult
from app.models.users import (
    AccountAuthorizationRead,
    AccountCapabilities,
    AccountProfileRead,
    AccountProfileUpdate,
    UserRead,
)
from app.utils.auth_utils import authorize_user
from app.utils.permissions import (
    Permission,
    PlatformRole,
    Principal,
    has_global_permission,
    resolve_authorization_context,
)
from app.utils.user_utils import assert_username_available, assign_projects_to_user, find_mf_user_proposals

router = APIRouter(tags=["account"])
engine = get_engine()


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
    with Session(engine) as session:
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
    with Session(engine) as session:
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
            },
        )


@router.patch("/account/profile", response_model=UserRead)
def update_account_profile(auth_res: Annotated[Principal, Depends(authorize_user)], user_update: AccountProfileUpdate):
    """Partially update the authenticated caller's own profile."""
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        update_data = user_update.model_dump(exclude_unset=True)
        if "username" in update_data and update_data["username"] is not None:
            assert_username_available(session, update_data["username"], exclude_orcid=user_unique_id)
        for key, value in update_data.items():
            setattr(user, key, value)
        session.add(user)
        session.commit()
        session.refresh(user)
        return _self_user_read(user)


@router.post("/account/sync-projects", response_model=ProjectSyncResult)
def sync_account_projects(auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Add project memberships discovered from the MF proposal database."""
    user_unique_id = auth_res.user_id
    proposal_ids = find_mf_user_proposals(user_unique_id) if is_orcid(user_unique_id) else []
    with Session(engine) as session:
        result = assign_projects_to_user(session, user_unique_id, proposal_ids)
        session.commit()
    return ProjectSyncResult(orcid=user_unique_id, **result)


@router.get("/account/join_requests", response_model=PaginatedResponse[JoinRequestRead])
def get_account_join_requests(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    status: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """Return the authenticated caller's own join request history across all groups.
    Filter by ?status=pending|approved|rejected."""
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        query = select(AccessGroupJoinRequest).where(AccessGroupJoinRequest.requester_id == user_unique_id)
        if status:
            query = query.where(AccessGroupJoinRequest.status == status)

        total = session.exec(select(func.count()).select_from(query.subquery())).one()
        items = session.exec(
            query.order_by(AccessGroupJoinRequest.id.desc()).offset(offset).limit(limit)
        ).all()
        return PaginatedResponse(total=total, limit=limit, offset=offset, items=items)


@router.get("/account/apikey", response_model=ApiKeyRead)
def get_account_apikey(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    response: Response,
):
    """Return the authenticated caller's current API key."""
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        token = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == user_unique_id)
            .order_by(ApiToken.token_created.desc())
        ).first()
        if token is None:
            raise HTTPException(status_code=404, detail="No API key found")
        response.headers["Cache-Control"] = "no-store"
        return ApiKeyRead(api_key=token.token)


@router.get("/account/verify", response_model=ApiKeyVerify)
def verify_account_apikey(auth_res: Annotated[Principal, Depends(authorize_user)]):
    """
    Verify the caller's API key and return its validity and expiry date.
    Returns 401 for invalid or expired keys (handled by authorize_user before this runs).
    """
    user_unique_id = auth_res.user_id
    with Session(engine) as session:
        token = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == user_unique_id)
            .order_by(ApiToken.token_created.desc())
        ).first()
        if token is None:
            raise HTTPException(status_code=404, detail="No API key found")

        created_at = token.token_created
        if isinstance(created_at, str):
            created_dt = datetime.strptime(created_at, "%Y-%m-%d")
        else:
            created_dt = created_at

        expires_dt = created_dt + timedelta(days=365)
        return ApiKeyVerify(
            valid=True,
            created_at=created_dt.date().isoformat(),
            expires_at=expires_dt.date().isoformat(),
        )
