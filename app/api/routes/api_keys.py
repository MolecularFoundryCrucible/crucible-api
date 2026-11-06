from datetime import datetime, timezone
from typing import Annotated, List, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import UserRef
from app.auth.api_keys import KEY_LIFETIME, MAX_ACTIVE_KEYS, TooManyKeys, active, create_key, revoke_key, status_of
from app.models.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyListItem
from app.models.core_models import ApiKey
from app.utils.auth_utils import authorize_user, caller_row, forget_cached_key_hash, require_platform_admin, require_recent_hub_session
from app.utils.permissions import Principal
from app.utils.user_utils import resolve_user_by_identifier

router = APIRouter(tags=["account"])

_STEP_UP = {401: {"description": "A Hub session needs a recent sign-in (`insufficient_user_authentication`)"}, 403: {"description": "`hub_session_required`: an API key cannot create keys"}}
State = Literal["active", "inactive"]


def list_item(key: ApiKey) -> ApiKeyListItem:
    return ApiKeyListItem(id=key.id, name=key.name, last4=key.key_last4, status=status_of(key), created_at=key.created_at, last_used_at=key.last_used_at, expires_at=key.expires_at)


def _keys(session: Session, user_id: int, state: State) -> list[ApiKeyListItem]:
    query = select(ApiKey).where(ApiKey.user_id == user_id)
    query = query.where(active()) if state == "active" else query.where(~active())
    return [list_item(key) for key in session.exec(query.order_by(ApiKey.created_at.desc(), ApiKey.id.desc())).all()]


def _revoke(session: Session, user_id: int, key_id: int, actor) -> None:
    key = session.get(ApiKey, key_id)
    if key is None or key.user_id != user_id:
        raise HTTPException(status_code=404, detail="No such API key")
    revoke_key(session, key, actor=actor)
    session.commit()
    forget_cached_key_hash(key.key_hash)


@router.post("/account/apikeys", response_model=ApiKeyCreated, status_code=201, responses={**_STEP_UP, 409: {"description": f"`too_many_keys`: at most {MAX_ACTIVE_KEYS} active keys"}, 422: {"description": "expires_at in the past or more than a year away"}})
def create_account_apikey(body: ApiKeyCreate, response: Response, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Create another API key. The key is shown only in this response. Needs a Hub session with a recent sign-in."""
    require_recent_hub_session(auth_res, "Creating an API key needs a Hub session, not an API key")
    now = datetime.now(timezone.utc)
    if body.expires_at is not None and not now < body.expires_at <= now + KEY_LIFETIME:
        raise HTTPException(status_code=422, detail="expires_at must be in the future and at most a year away")
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        try:
            key, secret = create_key(session, user, name=body.name, expires_at=body.expires_at, now=now)
        except TooManyKeys:
            raise HTTPException(status_code=409, detail={"code": "too_many_keys", "message": f"At most {MAX_ACTIVE_KEYS} keys can be active; revoke one first"})
        session.commit()
        session.refresh(key)
        response.headers["Cache-Control"] = "no-store"
        return ApiKeyCreated(**list_item(key).model_dump(), api_key=secret)


@router.get("/account/apikeys", response_model=List[ApiKeyListItem])
def list_account_apikeys(auth_res: Annotated[Principal, Depends(authorize_user)], state: State = Query("active", description="active, or inactive for expired and revoked keys")):
    """Your API keys, never the keys themselves."""
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return _keys(session, user.id, state)


@router.delete("/account/apikeys/{key_id}", status_code=204, responses={404: {"description": "Not one of your keys"}})
def revoke_account_apikey(key_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Revoke one of your API keys. It stops working within a minute everywhere, at once on this server."""
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        _revoke(session, user.id, key_id, actor=user)
    return Response(status_code=204)


@router.get("/users/{user_ref}/apikeys", response_model=List[ApiKeyListItem])
def list_user_apikeys(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)], state: State = Query("active", description="active, or inactive for expired and revoked keys")):
    """A person's API keys, never the keys themselves. Platform administrators only."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session:
        return _keys(session, resolve_user_by_identifier(session, user_ref).id, state)


@router.delete("/users/{user_ref}/apikeys/{key_id}", status_code=204, responses={404: {"description": "Not one of this person's keys"}})
def revoke_user_apikey(user_ref: UserRef, key_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Revoke one of a person's API keys. Platform administrators only."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session:
        user = resolve_user_by_identifier(session, user_ref)
        _revoke(session, user.id, key_id, actor=caller_row(session, auth_res))
    return Response(status_code=204)
