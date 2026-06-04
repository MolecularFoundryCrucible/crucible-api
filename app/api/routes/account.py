from datetime import timedelta, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import ApiToken, User
from app.models.response_schemas import AccountInfo
from app.models.users import AccountProfileUpdate, UserRead
from app.utils.auth_utils import authorize_user

router = APIRouter(tags=["account"])
engine = get_engine()


@router.get("/account", response_model=AccountInfo)
def get_account_info(auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Return auth context for the authenticated caller: ORCID, access groups, and profile."""
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        user_info = UserRead.model_validate(user).model_dump() if user else {}
    return AccountInfo(user_unique_id=user_unique_id, access_group_ids=ag_ids, user_info=user_info)


@router.get("/account/profile", response_model=UserRead)
def get_account_profile(auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Return the authenticated caller's user profile."""
    user_unique_id, _ = auth_res
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return user


@router.patch("/account/profile", response_model=UserRead)
def update_account_profile(auth_res: Annotated[tuple, Depends(authorize_user)], user_update: AccountProfileUpdate):
    """Partially update the authenticated caller's own profile."""
    user_unique_id, _ = auth_res
    with Session(engine) as session:
        user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        for key, value in user_update.model_dump(exclude_unset=True).items():
            setattr(user, key, value)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


@router.get("/account/apikey")
def get_account_apikey(auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Return the authenticated caller's current API key."""
    user_unique_id, _ = auth_res
    with Session(engine) as session:
        token = session.exec(
            select(ApiToken).where(ApiToken.user_unique_id == user_unique_id)
            .order_by(ApiToken.token_created.desc())
        ).first()
        if token is None:
            raise HTTPException(status_code=404, detail="No API key found")
        return {"api_key": token.token}


@router.get("/account/verify")
def verify_account_apikey(auth_res: Annotated[tuple, Depends(authorize_user)]):
    """
    Verify the caller's API key and return its validity and expiry date.
    Returns 401 for invalid or expired keys (handled by authorize_user before this runs).
    """
    user_unique_id, _ = auth_res
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
        return {
            "valid": True,
            "created_at": created_dt.date().isoformat(),
            "expires_at": expires_dt.date().isoformat(),
        }
