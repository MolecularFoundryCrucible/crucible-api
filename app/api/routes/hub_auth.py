import os
import secrets

import requests
from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from joserfc.errors import JoseError
from pydantic import BaseModel, Field

from app.api.dependencies import get_engine
from app.api.dependencies import get_api_root_path
from app.utils.hub_login import (
    HubLoginUnavailable, InvalidHubLogin, consume_login, create_handoff_code,
    create_login, exchange_handoff_code, load_login_config,
)
from app.utils.hub_orcid import exchange_orcid_code, orcid_authorization_url
from app.utils.hub_tokens import InvalidHubGrant, HubTokenPair, refresh_hub_tokens, revoke_hub_tokens
from app.utils.hub_secrets import HubSecretUnavailable, get_hub_secret, hub_auth_enabled
from app.models.timestamps import UtcDatetime


router = APIRouter(prefix="/auth/hub", tags=["auth"])
client_security = HTTPBasic(auto_error=False)
LOGIN_COOKIE = "crucible_hub_login"


class HubCodeExchangeRequest(BaseModel):
    code: str = Field(min_length=32, max_length=128)
    code_verifier: str = Field(min_length=43, max_length=128)
    redirect_uri: str = Field(min_length=1, max_length=512)


class HubRefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=50, max_length=128)


class HubRevokeRequest(BaseModel):
    token: str = Field(min_length=50, max_length=128)


class HubTokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    access_expires_at: UtcDatetime
    refresh_expires_at: UtcDatetime
    user_unique_id: str


def require_hub_client(credentials: HTTPBasicCredentials | None = Depends(client_security)) -> str:
    if not hub_auth_enabled():
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable")
    configured_id = os.getenv("HUB_AUTH_CLIENT_ID", "crucible-hub")
    try:
        configured_secret = get_hub_secret("HUB_AUTH_CLIENT_SECRET")
    except HubSecretUnavailable:
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable") from None
    if not configured_secret:
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable")
    if credentials is None or not (
        secrets.compare_digest(credentials.username, configured_id)
        and secrets.compare_digest(credentials.password, configured_secret)
    ):
        raise HTTPException(status_code=401, detail="Invalid client credentials", headers={"WWW-Authenticate": "Basic"})
    return configured_id


def _token_response(pair: HubTokenPair, response: Response) -> HubTokenResponse:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return HubTokenResponse(
        access_token=pair.access_token,
        refresh_token=pair.refresh_token,
        access_expires_at=pair.access_expires_at,
        refresh_expires_at=pair.refresh_expires_at,
        user_unique_id=pair.user_unique_id,
    )


def _browser_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


@router.get("/login", response_class=RedirectResponse, status_code=302, responses={400: {"description": "Invalid sign-in request"}, 503: {"description": "Hub sign-in unavailable"}})
def begin_hub_login(
    client_id: str = Query(min_length=1, max_length=100),
    redirect_uri: str = Query(min_length=1, max_length=512),
    state: str = Query(min_length=16, max_length=256),
    code_challenge: str = Query(min_length=43, max_length=43),
    code_challenge_method: str = Query(pattern="^S256$"),
    max_age: int | None = Query(default=None, ge=0, le=86400, description="Maximum seconds since the user's last ORCID authentication. 0 forces ORCID to re-authenticate; use it before credential-creating operations that require a recent sign-in."),
    engine=Depends(get_engine),
) -> RedirectResponse:
    try:
        config = load_login_config()
        orcid_state, browser_secret, nonce = create_login(engine, config, client_id, redirect_uri, state, code_challenge)
        destination = orcid_authorization_url(config, orcid_state, nonce, max_age)
    except HubLoginUnavailable:
        raise HTTPException(status_code=503, detail="Hub sign-in is unavailable")
    except InvalidHubLogin:
        raise HTTPException(status_code=400, detail="Invalid sign-in request")
    response = _browser_headers(RedirectResponse(destination, status_code=302))
    response.set_cookie(
        LOGIN_COOKIE,
        browser_secret,
        max_age=600,
        path=f"{get_api_root_path()}/auth/hub",
        secure=config.public_api_url.startswith("https://"),
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/callback", response_class=RedirectResponse, status_code=302, responses={400: {"description": "Sign-in failed"}})
def finish_hub_login(
    request: Request,
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=256),
    engine=Depends(get_engine),
) -> Response:
    cookie_path = f"{get_api_root_path()}/auth/hub"
    try:
        config = load_login_config()
        transaction = consume_login(engine, state or "", request.cookies.get(LOGIN_COOKIE))
        if error is not None or code is None:
            raise InvalidHubLogin
        orcid, given_name, family_name, auth_time = exchange_orcid_code(config, code, transaction.nonce)
        _, destination = create_handoff_code(engine, transaction, orcid, given_name, family_name, auth_time=auth_time)
        response = _browser_headers(RedirectResponse(destination, status_code=302))
    except (HubLoginUnavailable, InvalidHubLogin, OAuthError, JoseError, requests.RequestException, ValueError, KeyError):
        response = _browser_headers(PlainTextResponse("Sign-in failed. Please return to the Hub and try again.", status_code=400))
    response.delete_cookie(LOGIN_COOKIE, path=cookie_path)
    return response


@router.post("/token", response_model=HubTokenResponse, responses={401: {"description": "Invalid client or sign-in code"}, 503: {"description": "Hub sign-in unavailable"}})
def exchange_hub_code(
    body: HubCodeExchangeRequest,
    response: Response,
    client_id: str = Depends(require_hub_client),
    engine=Depends(get_engine),
) -> HubTokenResponse:
    try:
        pair = exchange_handoff_code(engine, body.code, body.code_verifier, client_id, body.redirect_uri)
    except InvalidHubGrant:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
    return _token_response(pair, response)


@router.post("/refresh", response_model=HubTokenResponse, responses={401: {"description": "Invalid client or refresh token"}, 503: {"description": "Hub sign-in unavailable"}})
def refresh_hub_credential(
    body: HubRefreshRequest,
    response: Response,
    client_id: str = Depends(require_hub_client),
    engine=Depends(get_engine),
) -> HubTokenResponse:
    try:
        pair = refresh_hub_tokens(engine, body.refresh_token, client_id)
    except InvalidHubGrant:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    return _token_response(pair, response)


@router.post(
    "/revoke",
    status_code=204,
    description="Requires HTTP Basic Hub client authentication and JSON containing token. Revokes the entire login token family, including all access and refresh generations. A previously rotated refresh token can revoke the family. Repeated requests, unknown tokens, and tokens belonging to another client return 204 without disclosing token validity. Subsequent authentication checks reject revoked tokens; already-authorized requests may finish. Other login sessions, legacy API keys, and the ORCID session are unaffected. The Hub must also delete its own session and browser cookie.",
    responses={204: {"description": "Revocation processed; no-store response"}, 401: {"description": "Invalid client"}, 503: {"description": "Hub sign-in unavailable"}},
)
def revoke_hub_credential(
    body: HubRevokeRequest,
    client_id: str = Depends(require_hub_client),
    engine=Depends(get_engine),
) -> Response:
    revoke_hub_tokens(engine, body.token, client_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
