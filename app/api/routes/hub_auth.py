import logging
import secrets
from pathlib import Path as FilePath
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.api.dependencies import get_engine
from app.api.dependencies import get_api_root_path
from app.auth.providers import TOKEN_FAILURES, ProviderUnavailable, authorization_url, new_code_verifier, verified_identity
from app.config import ConfigurationError, OidcProvider, get_settings, optional_secret
from app.models.core_models import HubLoginTransaction
from app.utils.hub_login import (
    HubLoginRefused, HubLoginUnavailable, InvalidHubLogin, check_login_request, consume_login, create_handoff_code,
    create_login, exchange_handoff_code, hub_auth_enabled, hub_error_destination, load_login_config,
)
from app.utils.hub_tokens import InvalidHubGrant, HubTokenPair, refresh_hub_tokens, revoke_hub_tokens
from app.models.timestamps import UtcDatetime


router = APIRouter(prefix="/auth/hub", tags=["auth"])
client_security = HTTPBasic(auto_error=False)
LOGIN_COOKIE = "crucible_hub_login"
_PROVIDER_ERRORS = frozenset({"access_denied", "temporarily_unavailable"})
_templates = Jinja2Templates(directory=FilePath(__file__).resolve().parents[2] / "templates")
logger = logging.getLogger(__name__)


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
    auth_time: UtcDatetime | None = None


def require_hub_client(credentials: HTTPBasicCredentials | None = Depends(client_security)) -> str:
    if not hub_auth_enabled():
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable")
    configured_id = get_settings().hub.client_id
    try:
        configured_secret = optional_secret("HUB_AUTH_CLIENT_SECRET")
    except ConfigurationError:
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
        auth_time=pair.auth_time,
    )


def _browser_headers(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _hub_error(callback_uri: str, hub_state: str, error: str) -> Response:
    return _browser_headers(RedirectResponse(hub_error_destination(callback_uri, hub_state, error), status_code=302))


def _chooser(request: Request, listed: list[OidcProvider], hub_request: dict[str, str | int | None]) -> Response:
    """Starts nothing: each link returns to this route with a provider, which then starts that sign-in."""
    known = {name: value for name, value in hub_request.items() if value is not None}
    methods = [{"display_name": provider.display_name, "href": f"?{urlencode({**known, 'provider': provider.key})}"} for provider in listed]
    nonce = secrets.token_urlsafe(16)
    response = _templates.TemplateResponse(request, "hub_sign_in_chooser.html", {"methods": methods, "style_nonce": nonce})
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


@router.get("/login", response_class=RedirectResponse, status_code=302, responses={200: {"description": "A page to choose how to sign in, when several providers are listed and none is named", "content": {"text/html": {}}}, 302: {"description": "Redirect to the provider, or to the Hub with an error when the provider cannot be reached"}, 400: {"description": "Invalid sign-in request"}, 503: {"description": "Hub sign-in unavailable"}})
async def begin_hub_login(
    request: Request,
    client_id: str = Query(min_length=1, max_length=100),
    redirect_uri: str = Query(min_length=1, max_length=512),
    state: str = Query(min_length=16, max_length=256),
    code_challenge: str = Query(min_length=43, max_length=43),
    code_challenge_method: str = Query(pattern="^S256$"),
    max_age: int | None = Query(default=None, ge=0, le=86400, description="Maximum seconds since the user's last authentication at the provider. 0 forces the provider to re-authenticate; use it before credential-creating operations that require a recent sign-in."),
    provider: str | None = Query(default=None, max_length=32, description="The provider to use, a key from GET /auth/methods. When omitted, the only listed provider is used, or a page offers the choice."),
    required_user: str | None = Query(default=None, min_length=1, max_length=255, description="The unique_id that must sign in. Any other account gets error=login_required and no code. It passes through the browser, so the Hub must still compare the account it receives."),
    engine=Depends(get_engine),
) -> Response:
    try:
        config = await run_in_threadpool(load_login_config)
        check_login_request(config, client_id, redirect_uri, state, code_challenge)
    except HubLoginUnavailable:
        raise HTTPException(status_code=503, detail="Hub sign-in is unavailable")
    except InvalidHubLogin:
        raise HTTPException(status_code=400, detail="Invalid sign-in request")
    listed = get_settings().auth.providers
    if provider is None and len(listed) > 1:
        return _chooser(request, listed, {
            "client_id": client_id, "redirect_uri": redirect_uri, "state": state, "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method, "max_age": max_age, "required_user": required_user,
        })
    chosen = listed[0] if provider is None else get_settings().auth.provider(provider)
    if chosen is None:
        raise HTTPException(status_code=400, detail="Invalid sign-in request")
    try:
        code_verifier = await new_code_verifier(chosen)
        provider_state, browser_secret, nonce = await run_in_threadpool(create_login, engine, config, client_id, redirect_uri, state, code_challenge, provider_key=chosen.key, code_verifier=code_verifier, required_user=required_user)
        destination = await authorization_url(chosen, config.provider_callback_uri(chosen), provider_state, nonce, max_age, code_verifier)
    except (ProviderUnavailable, ConfigurationError, httpx.HTTPError) as error:
        logger.warning("Hub sign-in could not reach provider %s: %s", chosen.key, type(error).__name__)
        return _hub_error(redirect_uri, state, "temporarily_unavailable")
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


def _callback_provider(transaction: HubLoginTransaction, served: OidcProvider | None) -> OidcProvider:
    """The provider this callback address serves, if it is the one the sign-in started with. A row
    with no key came from a pod older than the key, when only the legacy provider existed."""
    auth = get_settings().auth
    started = auth.provider(transaction.provider_key) if transaction.provider_key else auth.hub_callback_provider
    if served is None or started is None or served.key != started.key:
        raise HubLoginRefused("access_denied")
    return served


async def _finish_sign_in(request: Request, engine, served_by, code: str | None, state: str | None, error: str | None) -> Response:
    try:
        config = await run_in_threadpool(load_login_config)
        transaction = await run_in_threadpool(consume_login, engine, state or "", request.cookies.get(LOGIN_COOKIE))
    except (HubLoginUnavailable, InvalidHubLogin):
        response = _browser_headers(PlainTextResponse("Sign-in failed. Please return to the Hub and try again.", status_code=400))
    else:
        try:
            provider = _callback_provider(transaction, served_by(get_settings().auth))
            if error is not None:
                raise HubLoginRefused(error if error in _PROVIDER_ERRORS else "server_error")
            if code is None:
                raise HubLoginRefused("server_error")
            identity = await verified_identity(provider, config.provider_callback_uri(provider), code, transaction.nonce, transaction.provider_code_verifier)
            _, destination = await run_in_threadpool(create_handoff_code, engine, transaction, provider, identity.subject, identity.given_name, identity.family_name, auth_time=identity.auth_time)
            response = _browser_headers(RedirectResponse(destination, status_code=302))
        except HubLoginRefused as refused:
            logger.warning("Hub sign-in refused: %s", refused.error)
            response = _hub_error(transaction.callback_uri, transaction.hub_state, refused.error)
        except (ProviderUnavailable, ConfigurationError, httpx.HTTPError) as failure:
            logger.warning("Hub sign-in could not reach the provider: %s", type(failure).__name__)
            response = _hub_error(transaction.callback_uri, transaction.hub_state, "temporarily_unavailable")
        except TOKEN_FAILURES as failure:
            logger.warning("Hub sign-in failed: %s", type(failure).__name__)
            response = _hub_error(transaction.callback_uri, transaction.hub_state, "server_error")
    response.delete_cookie(LOGIN_COOKIE, path=f"{get_api_root_path()}/auth/hub")
    return response


_CALLBACK_RESPONSES = {302: {"description": "Redirect to the Hub with a sign-in code, or with state and an RFC 6749 error"}, 400: {"description": "The sign-in cannot be matched to this browser, so nothing is trusted enough to redirect to"}}


@router.get("/callback", response_class=RedirectResponse, status_code=302, responses=_CALLBACK_RESPONSES)
async def finish_hub_login(
    request: Request,
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=256),
    engine=Depends(get_engine),
) -> Response:
    return await _finish_sign_in(request, engine, lambda auth: auth.hub_callback_provider, code, state, error)


@router.get("/oidc/{key}/callback", response_class=RedirectResponse, status_code=302, responses=_CALLBACK_RESPONSES)
async def finish_provider_login(
    request: Request,
    key: str = Path(max_length=32),
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=256),
    engine=Depends(get_engine),
) -> Response:
    def served_by(auth) -> OidcProvider | None:
        provider = auth.provider(key)
        return provider if provider is not None and not provider.legacy_callback else None

    return await _finish_sign_in(request, engine, served_by, code, state, error)


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
