from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup, User, UserAccessGroupLink
from app.models.users import ServiceAccountCreate, ServiceAccountRead
from app.utils.auth_utils import authorize_user, find_or_add_api_access, require_platform_admin
from app.utils.permissions import Principal
from app.utils.sql_utils import add_or_update
from app.utils.user_utils import assert_username_available

router = APIRouter(tags=["service_accounts"])
engine = get_engine()


@router.post("/service_accounts", response_model=ServiceAccountRead)
def create_service_account(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    body: ServiceAccountCreate,
):
    '''
    Create a service account and return its API key.
    The API key is only returned once — store it securely.
    unique_id is optional; if omitted the server generates an mfid.
    Admin only.
    '''
    require_platform_admin(auth_res)

    with Session(engine) as session:
        # Username must be unique across all users
        assert_username_available(session, body.username)

        # If unique_id provided, check it doesn't already exist
        if body.unique_id:
            if session.exec(select(User).where(User.unique_id == body.unique_id)).one_or_none():
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
        )
        session.add(user)
        session.flush()  # get user.id

        # Create personal access group (same as regular users at login)
        ag = add_or_update(session, AccessGroup,
                           AccessGroup.group_name == unique_id,
                           group_name=unique_id)
        session.flush()  # ensure ag.id is populated before creating the link
        link = UserAccessGroupLink(user_id=user.id, accessgroup_id=ag.id)
        session.add(link)
        session.commit()

    # Generate API key (outside session — find_or_add_api_access opens its own)
    api_key = find_or_add_api_access(unique_id, engine, regenerate=False)

    return ServiceAccountRead(
        unique_id=unique_id,
        username=body.username,
        api_key=api_key,
    )


@router.post("/service_accounts/{unique_id}/rotate_key", response_model=ServiceAccountRead)
def rotate_service_account_key(
    auth_res: Annotated[Principal, Depends(authorize_user)],
    unique_id: str,
):
    '''
    Generate a new API key for a service account, invalidating the previous one.
    The new key is only returned once — store it securely.
    Admin only.
    '''
    require_platform_admin(auth_res)

    with Session(engine) as session:
        user = session.exec(
            select(User).where(User.unique_id == unique_id, User.is_service_account == True)
        ).one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="Service account not found.")
        username = user.username

    api_key = find_or_add_api_access(unique_id, engine, regenerate=True)

    return ServiceAccountRead(
        unique_id=unique_id,
        username=username,
        api_key=api_key,
    )
