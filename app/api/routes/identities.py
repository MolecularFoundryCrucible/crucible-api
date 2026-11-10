from typing import Annotated, List

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.concurrency import run_in_threadpool
from sqlmodel import Session

from app.api.dependencies import get_engine
from app.auth.linking import LastSignInMethod, LinkingUnavailable, TooManyLinks, begin_link, identity_read, require_linking_available, sign_in_methods, unlink_identity
from app.auth.providers import ProviderUnavailable, new_code_verifier
from app.config import ConfigurationError, get_settings
from app.models.identities import IdentityRead, LinkStart, LinkStartRead
from app.api.path_params import UserRef
from app.utils.auth_utils import authorize_user, caller_row, require_human_admin_safeguard, require_platform_admin, require_recent_session, require_self_or_platform_admin
from app.utils.user_utils import person_by_reference, resolve_user_by_identifier
from app.auth.login_transactions import SignInUnavailable, InvalidLoginRequest, load_login_config
from app.utils.permissions import Principal

router = APIRouter(tags=["account"])

SESSION_ONLY = "Changing how you sign in needs a Hub session, not an API key"
LOOKUP_OFF = {"code": "identity_lookup_off", "message": "Linking is not available yet on this server"}
_STEP_UP = {401: {"description": "A Hub session needs a recent sign-in (`insufficient_user_authentication`)"}, 403: {"description": "`hub_session_required`: API keys cannot change how you sign in; `browser_sign_in_required`: nor can a session approved with a device code"}}


@router.get("/account/identities", response_model=List[IdentityRead])
def list_account_identities(auth_res: Annotated[Principal, Depends(authorize_user)]):
    """The ways you sign in."""
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        return [identity_read(identity) for identity in sign_in_methods(session, user)]


@router.post("/account/identities/{provider_key}/link", response_model=LinkStartRead, responses={**_STEP_UP, 400: {"description": "Unknown provider or invalid link request"}, 409: {"description": "`identity_lookup_off`"}, 429: {"description": "`too_many_links`: more than 5 links started in 10 minutes"}, 503: {"description": "Hub sign-in unavailable, or the provider cannot be reached"}})
async def start_account_link(provider_key: Annotated[str, Path(max_length=32)], body: LinkStart, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Start adding another way to sign in. Returns a one-time address on the API to send the
    person's browser to; the provider asks them to sign in, and the Hub then previews and confirms
    the link with POST /oauth/link/preview and /oauth/link/confirm."""
    require_recent_session(auth_res, SESSION_ONLY)
    try:
        require_linking_available()
        config = load_login_config()
    except LinkingUnavailable:
        raise HTTPException(status_code=409, detail=LOOKUP_OFF)
    except SignInUnavailable:
        raise HTTPException(status_code=503, detail="Hub sign-in is unavailable")
    provider = get_settings().auth.provider(provider_key)
    if provider is None:
        raise HTTPException(status_code=400, detail="Unknown provider")
    try:
        code_verifier = await new_code_verifier(provider)
        start = await run_in_threadpool(begin_link, get_engine(), config, auth_res.internal_user_id, auth_res.client_id, auth_res.session_family_id, provider, body.callback_uri, body.state, body.code_challenge, code_verifier=code_verifier)
    except InvalidLoginRequest:
        raise HTTPException(status_code=400, detail="Invalid link request")
    except TooManyLinks:
        raise HTTPException(status_code=429, detail={"code": "too_many_links", "message": "Too many links started recently; try again in a few minutes"}, headers={"Retry-After": "600"})
    except (ProviderUnavailable, ConfigurationError, httpx.HTTPError):
        raise HTTPException(status_code=503, detail="The provider cannot be reached")
    return LinkStartRead(authorization_url=f"{config.public_api_url}/oauth/link/start?token={start}")


@router.delete("/account/identities/{identity_id}", status_code=204, responses={**_STEP_UP, 404: {"description": "Not one of your logins"}, 409: {"description": "`last_sign_in_method`, or `identity_lookup_off`"}})
def unlink_account_identity(identity_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Remove one of your ways to sign in. Your other Hub sessions end; this one stays. Your last
    way to sign in cannot be removed. Signing in again with a removed login from a provider that
    lets anyone register creates a new, separate account."""
    require_recent_session(auth_res, SESSION_ONLY)
    try:
        require_linking_available()
    except LinkingUnavailable:
        raise HTTPException(status_code=409, detail=LOOKUP_OFF)
    with Session(get_engine()) as session:
        user = caller_row(session, auth_res, for_update=True)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        try:
            unlink_identity(session, user, identity_id, actor=user, keep_family_id=auth_res.session_family_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Not one of your logins")
        except LastSignInMethod:
            raise HTTPException(status_code=409, detail={"code": "last_sign_in_method", "message": "This is your last way to sign in; add another first"})
        session.commit()


ADMIN_SESSION_ONLY = "Changing how someone signs in needs a Hub session, not an API key"


def _admin_target(session: Session, user_ref: str, auth_res: Principal):
    require_platform_admin(auth_res)
    require_recent_session(auth_res, ADMIN_SESSION_ONLY)
    try:
        require_linking_available()
    except LinkingUnavailable:
        raise HTTPException(status_code=409, detail=LOOKUP_OFF)
    user = resolve_user_by_identifier(session, user_ref)
    if user.is_service_account:
        raise HTTPException(status_code=409, detail={"code": "service_account", "message": "Service accounts sign in with API keys, not logins"})
    require_human_admin_safeguard(auth_res, user)
    return user


@router.get("/users/{user_ref}/identities", response_model=List[IdentityRead])
def list_user_identities(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """A person's logins: your own, or anyone's for platform administrators."""
    with Session(get_engine()) as session:
        user = person_by_reference(session, user_ref, include_pending=True) if auth_res.is_platform_admin else None
        user = user or resolve_user_by_identifier(session, user_ref)
        require_self_or_platform_admin(auth_res, user)
        return [identity_read(identity) for identity in sign_in_methods(session, user)]


@router.delete("/users/{user_ref}/identities/{identity_id}", status_code=204, responses={**_STEP_UP, 404: {"description": "Not one of this person's logins"}, 409: {"description": "`last_sign_in_method`, `service_account`, or `identity_lookup_off`"}})
def remove_user_identity(user_ref: UserRef, identity_id: int, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Remove one of a person's logins, never their last. Ends all their Hub sessions. Platform
    administrators, from a Hub session with a recent sign-in."""
    with Session(get_engine()) as session:
        user = _admin_target(session, user_ref, auth_res)
        session.refresh(user, with_for_update=True)
        try:
            unlink_identity(session, user, identity_id, actor=caller_row(session, auth_res))
        except LookupError:
            raise HTTPException(status_code=404, detail="Not one of this person's logins")
        except LastSignInMethod:
            raise HTTPException(status_code=409, detail={"code": "last_sign_in_method", "message": "This is the person's last way to sign in"})
        session.commit()

