from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import and_, func, or_
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.api.path_params import UserRef
from app.auth.api_keys import KEY_LIFETIME, MAX_ACTIVE_KEYS, TooManyKeys, active, create_key, plain_key_hashes, revoke_key, status_of
from app.models.api_keys import ApiKeyCreate, ApiKeyCreated, ApiKeyListItem, ApiKeyRename
from app.tables import ApiKey
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, caller_row, forget_cached_key_hash, require_human_admin_safeguard, require_platform_admin, require_recent_hub_session
from app.utils.cursor_utils import CursorError, cursor_primary_value, filter_fingerprint, new_cursor, validate_cursor
from app.utils.permissions import Principal
from app.utils.user_utils import resolve_user_by_identifier

router = APIRouter(tags=["account"])

_STEP_UP = {401: {"description": "A Hub session needs a recent sign-in (`insufficient_user_authentication`)"}, 403: {"description": "`hub_session_required`: an API key cannot create keys; `browser_sign_in_required`: nor can a session approved with a device code"}}
State = Literal["active", "inactive"]
_STATE = Query("active", description="active, or inactive for expired and revoked keys")
_LIMIT = Query(50, ge=1, le=200)
_CURSOR = Query(None, min_length=1, max_length=4096, description="Opaque continuation cursor from next_cursor.")


def list_item(key: ApiKey, plain: set[str] = frozenset()) -> ApiKeyListItem:
    return ApiKeyListItem(id=key.id, name=key.name, last4=key.key_last4, status=status_of(key), created_at=key.created_at, last_used_at=key.last_used_at, expires_at=key.expires_at, revoked_at=key.revoked_at, older_routes=key.key_hash in plain)


def _keys(session: Session, principal: Principal, user_id: int, state: State, limit: int, cursor: str | None) -> PaginatedResponse[ApiKeyListItem]:
    """Newest first, by creation time and then id, in keyset pages."""
    digest = filter_fingerprint({"user_id": user_id, "state": state})
    query = select(ApiKey).where(ApiKey.user_id == user_id, active() if state == "active" else ~active())
    total = session.exec(select(func.count()).select_from(query.subquery())).one()
    if cursor is not None:
        try:
            decoded = validate_cursor(cursor, kind="api_key", sort="created", direction="desc", filters=digest, user_id=principal.user_id)
            if decoded is None:
                raise CursorError("Invalid cursor")
            created, key_id = cursor_primary_value(decoded), int(decoded.unique_id)
        except (CursorError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid cursor") from exc
        query = query.where(or_(ApiKey.created_at < created, and_(ApiKey.created_at == created, ApiKey.id < key_id)))
    rows = session.exec(query.order_by(ApiKey.created_at.desc(), ApiKey.id.desc()).limit(limit + 1)).all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = new_cursor(kind="api_key", sort="created", direction="desc", primary=last.created_at, unique_id=str(last.id), filters=digest, user_id=principal.user_id)
    plain = plain_key_hashes(session, user_id)
    return PaginatedResponse[ApiKeyListItem](total=total, limit=limit, items=[list_item(key, plain) for key in page], next_cursor=next_cursor)


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


@router.get("/account/apikeys", response_model=PaginatedResponse[ApiKeyListItem], responses={422: {"description": "Invalid cursor"}})
def list_account_apikeys(auth_res: Annotated[Principal, Depends(authorize_user)], state: State = _STATE, limit: int = _LIMIT, cursor: str | None = _CURSOR):
    """Your API keys, never the keys themselves."""
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return _keys(session, auth_res, user.id, state, limit, cursor)


@router.delete("/account/apikeys/{key_id}", status_code=204, responses={404: {"description": "Not one of your keys"}})
def revoke_account_apikey(key_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Revoke one of your API keys. It stops working within a minute everywhere, at once on this server."""
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        _revoke(session, user.id, key_id, actor=user)
    return Response(status_code=204)


@router.patch("/account/apikeys/{key_id}", response_model=ApiKeyListItem, responses={404: {"description": "Not one of your keys"}})
def rename_account_apikey(key_id: int, body: ApiKeyRename, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Rename one of your API keys."""
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        key = session.get(ApiKey, key_id)
        if user is None or key is None or key.user_id != user.id:
            raise HTTPException(status_code=404, detail="No such API key")
        key.name = body.name
        session.add(key)
        session.commit()
        session.refresh(key)
        return list_item(key, plain_key_hashes(session, user.id))


@router.get("/users/{user_ref}/apikeys", response_model=PaginatedResponse[ApiKeyListItem], responses={422: {"description": "Invalid cursor"}})
def list_user_apikeys(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)], state: State = _STATE, limit: int = _LIMIT, cursor: str | None = _CURSOR):
    """A person's API keys, never the keys themselves. Platform administrators only."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session:
        return _keys(session, auth_res, resolve_user_by_identifier(session, user_ref).id, state, limit, cursor)


@router.delete("/users/{user_ref}/apikeys/{key_id}", status_code=204, responses={404: {"description": "Not one of this person's keys"}})
def revoke_user_apikey(user_ref: UserRef, key_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Revoke one of a person's API keys. Platform administrators only; another human administrator's keys need a Hub session with a recent sign-in."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session:
        user = resolve_user_by_identifier(session, user_ref)
        require_human_admin_safeguard(auth_res, user)
        _revoke(session, user.id, key_id, actor=caller_row(session, auth_res))
    return Response(status_code=204)
