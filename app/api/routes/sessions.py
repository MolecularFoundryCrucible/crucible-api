from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.api.path_params import UserRef
from app.auth.sessions import ActiveSession, active_sessions, end_session, end_sessions
from app.models.identities import SessionRead
from app.models.pagination import PaginatedResponse
from app.utils.auth_utils import authorize_user, caller_row, require_human_admin_safeguard, require_platform_admin, require_recent_hub_session
from app.utils.cursor_utils import CursorError, cursor_primary_value, filter_fingerprint, new_cursor, validate_cursor
from app.utils.permissions import Principal
from app.utils.user_utils import resolve_user_by_identifier

router = APIRouter(tags=["account"])

_LIMIT = Query(50, ge=1, le=200)
_CURSOR = Query(None, min_length=1, max_length=4096, description="Opaque continuation cursor from next_cursor.")
_STEP_UP = {401: {"description": "A session needs a sign-in within the last 10 minutes (`insufficient_user_authentication`)"}, 403: {"description": "`hub_session_required`: an API key cannot end sessions; `browser_sign_in_required`: nor can a session approved with a device code"}}
SIGN_IN_AGAIN = "Ending sessions needs a signed-in session with a recent sign-in, not an API key"


def _read(item: ActiveSession, current: str | None) -> SessionRead:
    return SessionRead(id=item.id, client_id=item.client_id, client_name=item.client_name, created_at=item.created_at, last_used_at=item.last_used_at,
                       expires_at=item.expires_at, signed_in_at=item.auth_time, current=item.id == current)


def _page(session: Session, principal: Principal, user_id: int, limit: int, cursor: str | None) -> PaginatedResponse[SessionRead]:
    """Newest first; few enough per person to filter in Python, paged like the other lists."""
    items = active_sessions(session, user_id)
    digest = filter_fingerprint({"user_id": user_id, "kind": "sessions"})
    start = 0
    if cursor is not None:
        try:
            decoded = validate_cursor(cursor, kind="session", sort="created", direction="desc", filters=digest, user_id=principal.user_id)
            if decoded is None:
                raise CursorError("Invalid cursor")
            created, last_id = cursor_primary_value(decoded), decoded.unique_id
        except (CursorError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="Invalid cursor") from exc
        start = next((index for index, item in enumerate(items) if (item.created_at, item.id) < (created, last_id)), len(items))
    page = items[start:start + limit]
    next_cursor = None
    if start + limit < len(items):
        last = page[-1]
        next_cursor = new_cursor(kind="session", sort="created", direction="desc", primary=last.created_at, unique_id=last.id, filters=digest, user_id=principal.user_id)
    return PaginatedResponse[SessionRead](total=len(items), limit=limit, items=[_read(item, principal.session_family_id) for item in page], next_cursor=next_cursor)


def _caller(session: Session, principal: Principal):
    user = caller_row(session, principal)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/account/sessions", response_model=PaginatedResponse[SessionRead], responses={422: {"description": "Invalid cursor"}})
def list_account_sessions(auth_res: Annotated[Principal, Depends(authorize_user)], limit: int = _LIMIT, cursor: str | None = _CURSOR):
    """Your signed-in sessions: the Hub, the command line, devices you approved. Never their tokens.
    `current` marks the session making the request."""
    with Session(get_engine()) as session:
        return _page(session, auth_res, _caller(session, auth_res).id, limit, cursor)


@router.delete("/account/sessions/{session_id}", status_code=204, responses={**_STEP_UP, 404: {"description": "Not one of your active sessions"}})
def end_account_session(session_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """End one of your sessions at once; ending the current one signs you out. Needs a recent sign-in
    (OWASP ASVS 5.0 V7.5.2)."""
    require_recent_hub_session(auth_res, SIGN_IN_AGAIN)
    with Session(get_engine()) as session, session.begin():
        user = _caller(session, auth_res)
        if not end_session(session, user, session_id, actor=user):
            raise HTTPException(status_code=404, detail="No such session")
    return Response(status_code=204)


@router.delete("/account/sessions", status_code=204, responses=_STEP_UP)
def end_other_account_sessions(auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Sign out everywhere else: end every session but this one. Needs a recent sign-in."""
    require_recent_hub_session(auth_res, SIGN_IN_AGAIN)
    with Session(get_engine()) as session, session.begin():
        user = _caller(session, auth_res)
        end_sessions(session, user, actor=user, keep=auth_res.session_family_id)
    return Response(status_code=204)


@router.get("/users/{user_ref}/sessions", response_model=PaginatedResponse[SessionRead], responses={403: {"description": "Not a platform administrator"}, 422: {"description": "Invalid cursor"}})
def list_user_sessions(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)], limit: int = _LIMIT, cursor: str | None = _CURSOR):
    """A person's signed-in sessions. Platform administrators only."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session:
        return _page(session, auth_res, resolve_user_by_identifier(session, user_ref).id, limit, cursor)


@router.delete("/users/{user_ref}/sessions/{session_id}", status_code=204, responses={403: {"description": "Not a platform administrator, or `hub_session_required` for another human administrator"}, 404: {"description": "Not one of this person's active sessions"}})
def end_user_session(user_ref: UserRef, session_id: str, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """End one of a person's sessions (ASVS V7.4.5). Another human administrator's needs a recent sign-in."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session, session.begin():
        user = resolve_user_by_identifier(session, user_ref)
        if not auth_res.is_user(user):
            require_human_admin_safeguard(auth_res, user)
        if not end_session(session, user, session_id, actor=caller_row(session, auth_res)):
            raise HTTPException(status_code=404, detail="No such session")
    return Response(status_code=204)


@router.delete("/users/{user_ref}/sessions", status_code=204, responses={403: {"description": "Not a platform administrator, or `hub_session_required` for another human administrator"}})
def end_all_user_sessions(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """End all of a person's sessions (ASVS V7.4.5). Another human administrator's needs a recent sign-in."""
    require_platform_admin(auth_res)
    with Session(get_engine()) as session, session.begin():
        user = resolve_user_by_identifier(session, user_ref)
        if not auth_res.is_user(user):
            require_human_admin_safeguard(auth_res, user)
        end_sessions(session, user, actor=caller_row(session, auth_res), keep=auth_res.session_family_id if auth_res.is_user(user) else None)
    return Response(status_code=204)
