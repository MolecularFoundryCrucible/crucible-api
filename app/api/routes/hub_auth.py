import hashlib
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
from app.auth.providers import TOKEN_FAILURES, IssuerMismatch, ProviderUnavailable, authorization_url, check_response_issuer, new_code_verifier, verified_identity
from app.auth.clients import ClientUnavailable, authenticate_client, require_client_secrets
from app.config import ConfigurationError, OidcProvider, get_settings
from app.models.core_models import HubLoginTransaction
from app.auth.sign_in import AccountPending
from app.utils.hub_login import (
    HubLoginRefused, HubLoginUnavailable, InvalidHubLogin, check_login_request, consume_login, create_handoff_code,
    create_login, exchange_handoff_code, hub_auth_enabled, hub_error_destination, load_login_config,
)
from app.auth.linking import LinkRefused, LinkSessionStale, LinkingUnavailable, confirm_link, create_link_code, identity_read, open_link, preview_link, provider_fields
from app.config import ORCID_ISSUERS
from app.models.identities import IdentityRead, LinkCode, LinkPreviewRead
from app.utils.hub_tokens import InvalidHubGrant, HubTokenPair, refresh_hub_tokens, revoke_hub_tokens
from app.models.timestamps import UtcDatetime


router = APIRouter(prefix="/auth/hub", tags=["auth"])
client_security = HTTPBasic(auto_error=False)
LOGIN_COOKIE = "crucible_hub_login"


def login_cookie_name(state: str) -> str:
    """One cookie per sign-in, so parallel tabs and popups do not overwrite each other."""
    return f"{LOGIN_COOKIE}_{hashlib.sha256(state.encode()).hexdigest()[:16]}"


def _cookie_path() -> str:
    return f"{get_api_root_path()}/auth/hub"
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
    """The registered client these HTTP Basic credentials name, by its client_id."""
    if not hub_auth_enabled():
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable")
    try:
        require_client_secrets()
        client = authenticate_client(credentials.username, credentials.password) if credentials is not None else None
    except ClientUnavailable:
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable") from None
    if client is None:
        raise HTTPException(status_code=401, detail="Invalid client credentials", headers={"WWW-Authenticate": "Basic"})
    return client.client_id


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


def pending_page(request: Request) -> Response:
    """Shown only after a valid provider sign-in, so it reveals nothing to someone who has not proved who they are."""
    nonce = secrets.token_urlsafe(16)
    response = _templates.TemplateResponse(request, "hub_account_pending.html", {"style_nonce": nonce}, status_code=403)
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


def _chooser(request: Request, listed: list[OidcProvider], hub_request: dict[str, str | int | None]) -> Response:
    """Starts nothing: each link returns to this route with a provider, which then starts that sign-in."""
    known = {name: value for name, value in hub_request.items() if value is not None}
    methods = [{"display_name": provider.display_name, "is_orcid": provider.is_orcid, "href": f"?{urlencode({**known, 'provider': provider.key})}"} for provider in listed]
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
        login_cookie_name(provider_state),
        browser_secret,
        max_age=600,
        path=_cookie_path(),
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


async def _finish_sign_in(request: Request, engine, served_by, code: str | None, state: str | None, error: str | None, iss: str | None) -> Response:
    cookie = login_cookie_name(state or "")
    # Sign-ins started on a pod from before per-sign-in cookies set the shared name.
    browser_secret = request.cookies.get(cookie) or request.cookies.get(LOGIN_COOKIE)
    if request.cookies.get(cookie) is None:
        cookie = LOGIN_COOKIE
    try:
        config = await run_in_threadpool(load_login_config)
        transaction = await run_in_threadpool(consume_login, engine, state or "", browser_secret)
    except (HubLoginUnavailable, InvalidHubLogin):
        return _browser_headers(PlainTextResponse("Sign-in failed. Please return to the Hub and try again.", status_code=400))
    else:
        try:
            provider = _callback_provider(transaction, served_by(get_settings().auth))
            await check_response_issuer(provider, iss)
            if error is not None:
                raise HubLoginRefused(error if error in _PROVIDER_ERRORS else "server_error")
            if code is None:
                raise HubLoginRefused("server_error")
            identity = await verified_identity(provider, config.provider_callback_uri(provider), code, transaction.nonce, transaction.provider_code_verifier)
            if transaction.purpose == "link":
                link_code = await run_in_threadpool(create_link_code, engine, transaction, provider, identity.subject, identity.auth_time, identity.label)
                destination = f"{transaction.callback_uri}?{urlencode({'code': link_code, 'state': transaction.hub_state})}"
            else:
                _, destination = await run_in_threadpool(create_handoff_code, engine, transaction, provider, identity.subject, identity.given_name, identity.family_name, auth_time=identity.auth_time)
            response = _browser_headers(RedirectResponse(destination, status_code=302))
        except AccountPending:
            response = pending_page(request)
        except HubLoginRefused as refused:
            logger.warning("Hub sign-in refused: %s", refused.error)
            response = _hub_error(transaction.callback_uri, transaction.hub_state, refused.error)
        except IssuerMismatch as mismatch:
            logger.warning("Hub sign-in refused, the response did not come from the expected provider: %s", mismatch)
            response = _hub_error(transaction.callback_uri, transaction.hub_state, "access_denied")
        except (ProviderUnavailable, ConfigurationError, httpx.HTTPError) as failure:
            logger.warning("Hub sign-in could not reach the provider: %s", type(failure).__name__)
            response = _hub_error(transaction.callback_uri, transaction.hub_state, "temporarily_unavailable")
        except TOKEN_FAILURES as failure:
            logger.warning("Hub sign-in failed: %s", type(failure).__name__)
            response = _hub_error(transaction.callback_uri, transaction.hub_state, "server_error")
    response.delete_cookie(cookie, path=_cookie_path())
    return response


_CALLBACK_RESPONSES = {302: {"description": "Redirect to the Hub with a sign-in code, or with state and an RFC 6749 error"}, 400: {"description": "The sign-in cannot be matched to this browser, so nothing is trusted enough to redirect to"}}


@router.get("/callback", response_class=RedirectResponse, status_code=302, responses=_CALLBACK_RESPONSES)
async def finish_hub_login(
    request: Request,
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=256),
    iss: str | None = Query(default=None, max_length=512),
    engine=Depends(get_engine),
) -> Response:
    return await _finish_sign_in(request, engine, lambda auth: auth.hub_callback_provider, code, state, error, iss)


@router.get("/oidc/{key}/callback", response_class=RedirectResponse, status_code=302, responses=_CALLBACK_RESPONSES)
async def finish_provider_login(
    request: Request,
    key: str = Path(max_length=32),
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=256),
    iss: str | None = Query(default=None, max_length=512),
    engine=Depends(get_engine),
) -> Response:
    def served_by(auth) -> OidcProvider | None:
        provider = auth.provider(key)
        return provider if provider is not None and not provider.legacy_callback else None

    return await _finish_sign_in(request, engine, served_by, code, state, error, iss)


@router.get("/link/start", response_class=RedirectResponse, status_code=302, responses={302: {"description": "Redirect to the provider, which asks the person to sign in again, or to the Hub with an error"}, 400: {"description": "Unknown, used or expired start address"}})
async def start_hub_link(request: Request, token: str = Query(min_length=1, max_length=128), engine=Depends(get_engine)) -> Response:
    """Continue a link the Hub started: opened once, by the person's browser."""
    try:
        config = await run_in_threadpool(load_login_config)
        transaction, provider_state, browser_secret = await run_in_threadpool(open_link, engine, token)
    except (HubLoginUnavailable, InvalidHubLogin):
        return _browser_headers(PlainTextResponse("This link address is not valid any more. Return to the Hub and start again.", status_code=400))
    provider = get_settings().auth.provider(transaction.provider_key)
    if provider is None:
        return _hub_error(transaction.callback_uri, transaction.hub_state, "access_denied")
    try:
        destination = await authorization_url(provider, config.provider_callback_uri(provider), provider_state, transaction.nonce, 0, transaction.provider_code_verifier)
    except (ProviderUnavailable, ConfigurationError, httpx.HTTPError) as error:
        logger.warning("Linking could not reach provider %s: %s", provider.key, type(error).__name__)
        return _hub_error(transaction.callback_uri, transaction.hub_state, "temporarily_unavailable")
    response = _browser_headers(RedirectResponse(destination, status_code=302))
    response.set_cookie(login_cookie_name(provider_state), browser_secret, max_age=600, path=_cookie_path(), secure=config.public_api_url.startswith("https://"), httponly=True, samesite="lax")
    return response


_LINK_CODE_RESPONSES = {401: {"description": "Invalid client, link code or session, or `insufficient_user_authentication` when the session that started the link has no sign-in within 10 minutes"}, 409: {"description": "`identity_lookup_off`: linking waits for the identity lookup setting"}, 503: {"description": "Hub sign-in unavailable"}}


@router.post("/link/preview", response_model=LinkPreviewRead, responses=_LINK_CODE_RESPONSES)
def preview_hub_link(body: LinkCode, response: Response, client_id: str = Depends(require_hub_client), engine=Depends(get_engine)) -> LinkPreviewRead:
    """Name the login a link code would add, for the confirmation page. Changes nothing."""
    try:
        preview = preview_link(engine, body.code, body.code_verifier, client_id, body.redirect_uri, body.session_token)
    except LinkingUnavailable:
        raise HTTPException(status_code=409, detail={"code": "identity_lookup_off", "message": "Linking is not available yet on this server"})
    except LinkSessionStale:
        raise HTTPException(status_code=401, detail={"code": "insufficient_user_authentication", "message": "The session confirming this link needs a sign-in within the last 10 minutes"})
    except InvalidHubGrant:
        raise HTTPException(status_code=401, detail="Invalid or expired link code")
    response.headers["Cache-Control"] = "no-store"
    return LinkPreviewRead(**provider_fields(preview.issuer), label=preview.label, orcid=preview.subject if preview.issuer in ORCID_ISSUERS else None, status=preview.status)


@router.post("/link/confirm", response_model=IdentityRead, responses={**_LINK_CODE_RESPONSES, 409: {"description": "`identity_lookup_off`, or the preview status that refuses the link: `linked_elsewhere`, `second_orcid`, `orcid_of_another`"}})
def confirm_hub_link(body: LinkCode, response: Response, client_id: str = Depends(require_hub_client), engine=Depends(get_engine)) -> IdentityRead:
    """Add the login to the account of the person who started the link and is confirming it now."""
    try:
        identity = confirm_link(engine, body.code, body.code_verifier, client_id, body.redirect_uri, body.session_token)
    except LinkingUnavailable:
        raise HTTPException(status_code=409, detail={"code": "identity_lookup_off", "message": "Linking is not available yet on this server"})
    except LinkSessionStale:
        raise HTTPException(status_code=401, detail={"code": "insufficient_user_authentication", "message": "The session confirming this link needs a sign-in within the last 10 minutes"})
    except InvalidHubGrant:
        raise HTTPException(status_code=401, detail="Invalid or expired link code")
    except LinkRefused as refused:
        raise HTTPException(status_code=409, detail={"code": refused.reason, "message": "This login cannot be added to the account"})
    response.headers["Cache-Control"] = "no-store"
    return identity_read(identity)


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
