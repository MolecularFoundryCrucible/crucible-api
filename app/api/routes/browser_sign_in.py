import hashlib
import logging
import secrets
from pathlib import Path as FilePath
from urllib.parse import urlencode

import httpx
from sqlmodel import Session, select
from fastapi import APIRouter, Depends, Form, HTTPException, Path, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.api.dependencies import get_engine
from app.api.dependencies import get_api_root_path
from app.auth.providers import TOKEN_FAILURES, IssuerMismatch, ProviderUnavailable, authorization_url, check_response_issuer, new_code_verifier, verified_identity
from app.auth.clients import ClientUnavailable, authenticate_client, require_client_secrets
from app.auth.oauth_server import FormRequest, authorization_server, check_authorization_request
from authlib.oauth2 import OAuth2Error
from app.config import ConfigurationError, OidcProvider, get_settings
from app.tables import LoginTransaction
from app.auth.sign_in import AccountPending
from app.auth.login_transactions import (
    LoginRefused, SignInUnavailable, InvalidLoginRequest, check_login_request, consume_login, create_handoff_code,
    LOCAL_PROVIDER, client_redirect, create_local_code, create_login, sign_in_enabled, peek_login, error_destination, load_login_config, public_api_url,
)
from app.auth.linking import LinkRefused, LinkSessionStale, LinkingUnavailable, confirm_link, create_link_code, identity_read, open_link, preview_link, provider_fields
from app.config import ORCID_ISSUERS
from app.models.identities import IdentityRead, LinkCode, LinkPreviewRead
from app.auth.tokens import InvalidGrant, TokenPair
from app.models.timestamps import UtcDatetime


router = APIRouter(prefix="/auth/hub", tags=["auth"])
callback_router = APIRouter(prefix="/oauth/callback", tags=["auth"])
link_router = APIRouter(prefix="/oauth/link", tags=["auth"])
client_security = HTTPBasic(auto_error=False)
LOGIN_COOKIE = "crucible_hub_login"


def login_cookie_name(state: str) -> str:
    """One cookie per sign-in, so parallel tabs and popups do not overwrite each other."""
    return f"{LOGIN_COOKIE}_{hashlib.sha256(state.encode()).hexdigest()[:16]}"


def _cookie_path(provider: OidcProvider) -> str:
    """The sign-in cookie goes only to the address the provider sends the browser back to."""
    return f"{get_api_root_path()}{'/auth/hub' if provider.legacy_callback else '/oauth/callback'}"
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


def require_client(credentials: HTTPBasicCredentials | None = Depends(client_security)) -> str:
    """The registered client these HTTP Basic credentials name, by its client_id."""
    if not sign_in_enabled():
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable")
    try:
        require_client_secrets()
        client = authenticate_client(credentials.username, credentials.password) if credentials is not None else None
    except ClientUnavailable:
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable") from None
    if client is None:
        raise HTTPException(status_code=401, detail="Invalid client credentials", headers={"WWW-Authenticate": "Basic"})
    return client.client_id


def _server_request(request: Request, path: str, fields: list[tuple[str, str]], engine) -> FormRequest:
    try:
        base = public_api_url()
    except SignInUnavailable:
        raise HTTPException(status_code=503, detail="Hub authentication is unavailable") from None
    return FormRequest(f"{base}{path}", {"Authorization": request.headers["authorization"]}, fields, engine)


def _through_server(request: Request, path: str, fields: list[tuple[str, str]], engine) -> TokenPair | None:
    """The Hub's JSON routes run the same OAuth server as /oauth/token and keep their own answers."""
    prepared = _server_request(request, path, fields, engine)
    status, _, _ = authorization_server().create_token_response(prepared)
    return prepared.token_pair if status == 200 else None


def _token_response(pair: TokenPair, response: Response) -> HubTokenResponse:
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


def _deliver(request: Request, client_id: str, destination: str, callback_uri: str, hub_state: str, status: int = 302) -> Response:
    """Send the code back. A public client's identity cannot be verified, and a provider that
    remembers the person could finish the sign-in unseen, so the person confirms first
    (RFC 8252 8.6). The code stays in the page until they choose Allow."""
    from urllib.parse import urlsplit

    from app.auth.clients import client_by_id

    client = client_by_id(client_id)
    if client is None or not client.is_public:
        return _browser_headers(RedirectResponse(destination, status_code=status))
    nonce = secrets.token_urlsafe(16)
    response = _templates.TemplateResponse(request, "confirm_program.html", {
        "style_nonce": nonce, "client_name": client.display_name, "where": urlsplit(callback_uri).netloc,
        "allow": destination, "deny": error_destination(callback_uri, hub_state, "access_denied"),
    })
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


def _client_error(callback_uri: str, hub_state: str, error: str) -> Response:
    return _browser_headers(RedirectResponse(error_destination(callback_uri, hub_state, error), status_code=302))


def pending_page(request: Request) -> Response:
    """Shown only after a valid provider sign-in, so it reveals nothing to someone who has not proved who they are."""
    nonce = secrets.token_urlsafe(16)
    response = _templates.TemplateResponse(request, "account_pending.html", {"style_nonce": nonce}, status_code=403)
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


def _chooser(request: Request, listed: list[OidcProvider], hub_request: dict[str, str | int | None]) -> Response:
    """Starts nothing: each link returns to this route with a provider, which then starts that sign-in."""
    known = {name: value for name, value in hub_request.items() if value is not None}
    methods = [{"display_name": provider.display_name, "is_orcid": provider.is_orcid, "href": f"?{urlencode({**known, 'provider': provider.key})}"} for provider in listed]
    nonce = secrets.token_urlsafe(16)
    response = _templates.TemplateResponse(request, "sign_in_chooser.html", {"methods": methods, "style_nonce": nonce})
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


@router.get("/login", deprecated=True, response_class=RedirectResponse, status_code=302, responses={200: {"description": "A page to choose how to sign in, when several providers are listed and none is named", "content": {"text/html": {}}}, 302: {"description": "Redirect to the provider, or to the Hub with an error when the provider cannot be reached"}, 400: {"description": "Invalid sign-in request"}, 503: {"description": "Hub sign-in unavailable"}})
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
    """Deprecated: use GET /oauth/authorize with response_type=code, which this calls. Kept for Hub releases before 1.8.0."""
    return await start_sign_in(request, engine, path="/auth/hub/login", response_type="code", client_id=client_id, redirect_uri=redirect_uri, state=state,
                               code_challenge=code_challenge, code_challenge_method=code_challenge_method, max_age=max_age, provider=provider, required_user=required_user)


_AUTHORIZATION_PARAMETERS = frozenset({"response_type", "client_id", "redirect_uri", "state", "scope", "code_challenge", "code_challenge_method"})


async def start_sign_in(request: Request, engine, *, path: str, response_type: str | None, client_id: str | None, redirect_uri: str | None, state: str | None,
                        code_challenge: str | None, code_challenge_method: str | None, max_age: int | None, provider: str | None, required_user: str | None) -> Response:
    """Check an authorization request, then offer a choice of provider or send the browser to one.
    Errors go back to the client only once its redirect is verified (RFC 6749 4.1.2.1)."""
    try:
        config = await run_in_threadpool(load_login_config)
    except SignInUnavailable:
        raise HTTPException(status_code=503, detail="Hub sign-in is unavailable")
    standard = [(name, value) for name, value in request.query_params.multi_items() if name in _AUTHORIZATION_PARAMETERS and value]
    if path == "/auth/hub/login":
        standard.append(("response_type", "code"))
    try:
        check_authorization_request(f"{config.public_api_url}{path}", standard)
    except OAuth2Error as error:
        if error.redirect_uri is None or not state:
            raise HTTPException(status_code=400, detail="Invalid sign-in request")
        return _client_error(error.redirect_uri, state, error.error)
    try:
        if code_challenge_method != "S256":
            raise InvalidLoginRequest
        check_login_request(config, client_id, redirect_uri, state, code_challenge)
    except InvalidLoginRequest:
        if not redirect_uri or not state or not 16 <= len(state) <= 256:
            raise HTTPException(status_code=400, detail="Invalid sign-in request")
        return _client_error(redirect_uri, state, "invalid_request")
    listed = get_settings().auth.providers
    passwords = get_settings().auth.password_enabled
    if provider is None and len(listed) > 1 and not passwords:
        return _chooser(request, listed, {
            "response_type": response_type if path != "/auth/hub/login" else None,
            "client_id": client_id, "redirect_uri": redirect_uri, "state": state, "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method, "max_age": max_age, "required_user": required_user,
        })
    if provider == LOCAL_PROVIDER or (provider is None and (passwords or not listed)):
        if not passwords:
            raise HTTPException(status_code=400, detail="Invalid sign-in request")
        provider_state, browser_secret, _ = await run_in_threadpool(create_login, engine, config, client_id, redirect_uri, state, code_challenge, provider_key=LOCAL_PROVIDER, required_user=required_user)
        form = {"state": provider_state} | ({"max_age": max_age} if max_age is not None else {})
        response = _browser_headers(RedirectResponse(f"{config.public_api_url}/oauth/password?{urlencode(form)}", status_code=302))
        response.set_cookie(login_cookie_name(provider_state), browser_secret, max_age=600, path=_password_path(), secure=config.public_api_url.startswith("https://"), httponly=True, samesite="lax")
        return response
    chosen = listed[0] if provider is None else get_settings().auth.provider(provider)
    if chosen is None:
        raise HTTPException(status_code=400, detail="Invalid sign-in request")
    try:
        code_verifier = await new_code_verifier(chosen)
        provider_state, browser_secret, nonce = await run_in_threadpool(create_login, engine, config, client_id, redirect_uri, state, code_challenge, provider_key=chosen.key, code_verifier=code_verifier, required_user=required_user)
        destination = await authorization_url(chosen, config.provider_callback_uri(chosen), provider_state, nonce, max_age, code_verifier)
    except (ProviderUnavailable, ConfigurationError, httpx.HTTPError) as error:
        logger.warning("Hub sign-in could not reach provider %s: %s", chosen.key, type(error).__name__)
        return _client_error(redirect_uri, state, "temporarily_unavailable")
    response = _browser_headers(RedirectResponse(destination, status_code=302))
    response.set_cookie(
        login_cookie_name(provider_state),
        browser_secret,
        max_age=600,
        path=_cookie_path(chosen),
        secure=config.public_api_url.startswith("https://"),
        httponly=True,
        samesite="lax",
    )
    return response


def _callback_provider(transaction: LoginTransaction, served: OidcProvider | None) -> OidcProvider:
    """The provider this callback address serves, if it is the one the sign-in started with. A row
    with no key came from a pod older than the key, when only the legacy provider existed."""
    auth = get_settings().auth
    started = auth.provider(transaction.provider_key) if transaction.provider_key else auth.legacy_callback_provider
    if served is None or started is None or served.key != started.key:
        raise LoginRefused("access_denied")
    return served


async def _finish_sign_in(request: Request, engine, served_by, cookie_directory: str, code: str | None, state: str | None, error: str | None, iss: str | None) -> Response:
    cookie = login_cookie_name(state or "")
    # Sign-ins started on a pod from before per-sign-in cookies set the shared name.
    browser_secret = request.cookies.get(cookie) or request.cookies.get(LOGIN_COOKIE)
    if request.cookies.get(cookie) is None:
        cookie = LOGIN_COOKIE
    try:
        config = await run_in_threadpool(load_login_config)
        transaction = await run_in_threadpool(consume_login, engine, state or "", browser_secret)
    except (SignInUnavailable, InvalidLoginRequest):
        return _browser_headers(PlainTextResponse("Sign-in failed. Please return to the Hub and try again.", status_code=400))
    else:
        try:
            provider = _callback_provider(transaction, served_by(get_settings().auth))
            await check_response_issuer(provider, iss)
            if error is not None:
                raise LoginRefused(error if error in _PROVIDER_ERRORS else "server_error")
            if code is None:
                raise LoginRefused("server_error")
            identity = await verified_identity(provider, config.provider_callback_uri(provider), code, transaction.nonce, transaction.provider_code_verifier)
            if transaction.purpose == "link":
                link_code = await run_in_threadpool(create_link_code, engine, transaction, provider, identity.subject, identity.auth_time, identity.label)
                destination = client_redirect(transaction.callback_uri, {"code": link_code, "state": transaction.hub_state})
            else:
                _, destination = await run_in_threadpool(create_handoff_code, engine, transaction, provider, identity.subject, identity.given_name, identity.family_name, auth_time=identity.auth_time)
            response = _deliver(request, transaction.client_id, destination, transaction.callback_uri, transaction.hub_state) if transaction.purpose != "link" else _browser_headers(RedirectResponse(destination, status_code=302))
        except AccountPending:
            response = pending_page(request)
        except LoginRefused as refused:
            logger.warning("Hub sign-in refused: %s", refused.error)
            response = _client_error(transaction.callback_uri, transaction.hub_state, refused.error)
        except IssuerMismatch as mismatch:
            logger.warning("Hub sign-in refused, the response did not come from the expected provider: %s", mismatch)
            response = _client_error(transaction.callback_uri, transaction.hub_state, "access_denied")
        except (ProviderUnavailable, ConfigurationError, httpx.HTTPError) as failure:
            logger.warning("Hub sign-in could not reach the provider: %s", type(failure).__name__)
            response = _client_error(transaction.callback_uri, transaction.hub_state, "temporarily_unavailable")
        except TOKEN_FAILURES as failure:
            logger.warning("Hub sign-in failed: %s%s", type(failure).__name__, _provider_answer(failure, code))
            response = _client_error(transaction.callback_uri, transaction.hub_state, "server_error")
    response.delete_cookie(cookie, path=f"{get_api_root_path()}{cookie_directory}")
    return response


def _provider_answer(failure: Exception, code: str | None) -> str:
    """What the provider said, for the log: its OAuth error and description, with the one-time
    code removed, since some providers repeat it in the description."""
    error = getattr(failure, "error", None)
    if not isinstance(error, str) or not error:
        return ""
    description = getattr(failure, "description", None)
    if not isinstance(description, str) or not description:
        return f" ({error})"
    if code:
        description = description.replace(code, "[code]")
    return f" ({error}: {description[:200]})"


_CALLBACK_RESPONSES = {302: {"description": "Redirect to the Hub with a sign-in code, or with state and an RFC 6749 error"}, 400: {"description": "The sign-in cannot be matched to this browser, so nothing is trusted enough to redirect to"}}


@router.get("/callback", deprecated=True, response_class=RedirectResponse, status_code=302, responses=_CALLBACK_RESPONSES)
async def finish_hub_login(
    request: Request,
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=256),
    iss: str | None = Query(default=None, max_length=512),
    engine=Depends(get_engine),
) -> Response:
    """Deprecated: the provider callback for an ORCID application registered with this address (`legacy_callback`). New registrations use GET /oauth/callback/{key}."""
    return await _finish_sign_in(request, engine, lambda auth: auth.legacy_callback_provider, "/auth/hub", code, state, error, iss)


@callback_router.get("/{key}", response_class=RedirectResponse, status_code=302, responses=_CALLBACK_RESPONSES)
async def finish_provider_login(
    request: Request,
    key: str = Path(max_length=32),
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=256),
    iss: str | None = Query(default=None, max_length=512),
    engine=Depends(get_engine),
) -> Response:
    """Where a provider sends the browser back after sign-in: register `<API address>/oauth/callback/{key}` with the provider."""
    def served_by(auth) -> OidcProvider | None:
        provider = auth.provider(key)
        return provider if provider is not None and not provider.legacy_callback else None

    return await _finish_sign_in(request, engine, served_by, "/oauth/callback", code, state, error, iss)


@link_router.get("/start", response_class=RedirectResponse, status_code=302, responses={302: {"description": "Redirect to the provider, which asks the person to sign in again, or to the Hub with an error"}, 400: {"description": "Unknown, used or expired start address"}})
async def link_start(request: Request, token: str = Query(min_length=1, max_length=128), engine=Depends(get_engine)) -> Response:
    """Continue a link the Hub started: opened once, by the person's browser."""
    try:
        config = await run_in_threadpool(load_login_config)
        transaction, provider_state, browser_secret = await run_in_threadpool(open_link, engine, token)
    except (SignInUnavailable, InvalidLoginRequest):
        return _browser_headers(PlainTextResponse("This link address is not valid any more. Return to the Hub and start again.", status_code=400))
    provider = get_settings().auth.provider(transaction.provider_key)
    if provider is None:
        return _client_error(transaction.callback_uri, transaction.hub_state, "access_denied")
    try:
        destination = await authorization_url(provider, config.provider_callback_uri(provider), provider_state, transaction.nonce, 0, transaction.provider_code_verifier)
    except (ProviderUnavailable, ConfigurationError, httpx.HTTPError) as error:
        logger.warning("Linking could not reach provider %s: %s", provider.key, type(error).__name__)
        return _client_error(transaction.callback_uri, transaction.hub_state, "temporarily_unavailable")
    response = _browser_headers(RedirectResponse(destination, status_code=302))
    response.set_cookie(login_cookie_name(provider_state), browser_secret, max_age=600, path=_cookie_path(provider), secure=config.public_api_url.startswith("https://"), httponly=True, samesite="lax")
    return response


_LINK_CODE_RESPONSES = {401: {"description": "Invalid client, link code or session, or `insufficient_user_authentication` when the session that started the link has no sign-in within 10 minutes"}, 409: {"description": "`identity_lookup_off`: linking waits for the identity lookup setting"}, 503: {"description": "Hub sign-in unavailable"}}


@link_router.post("/preview", response_model=LinkPreviewRead, responses=_LINK_CODE_RESPONSES)
def link_preview(body: LinkCode, response: Response, client_id: str = Depends(require_client), engine=Depends(get_engine)) -> LinkPreviewRead:
    """Name the login a link code would add, for the confirmation page. Changes nothing."""
    try:
        preview = preview_link(engine, body.code, body.code_verifier, client_id, body.redirect_uri, body.session_token)
    except LinkingUnavailable:
        raise HTTPException(status_code=409, detail={"code": "identity_lookup_off", "message": "Linking is not available yet on this server"})
    except LinkSessionStale:
        raise HTTPException(status_code=401, detail={"code": "insufficient_user_authentication", "message": "The session confirming this link needs a sign-in within the last 10 minutes"})
    except InvalidGrant:
        raise HTTPException(status_code=401, detail="Invalid or expired link code")
    response.headers["Cache-Control"] = "no-store"
    return LinkPreviewRead(**provider_fields(preview.issuer), label=preview.label, orcid=preview.subject if preview.issuer in ORCID_ISSUERS else None, status=preview.status)


@link_router.post("/confirm", response_model=IdentityRead, responses={**_LINK_CODE_RESPONSES, 409: {"description": "`identity_lookup_off`, or the preview status that refuses the link: `linked_elsewhere`, `second_orcid`, `orcid_of_another`"}})
def link_confirm(body: LinkCode, response: Response, client_id: str = Depends(require_client), engine=Depends(get_engine)) -> IdentityRead:
    """Add the login to the account of the person who started the link and is confirming it now."""
    try:
        identity = confirm_link(engine, body.code, body.code_verifier, client_id, body.redirect_uri, body.session_token)
    except LinkingUnavailable:
        raise HTTPException(status_code=409, detail={"code": "identity_lookup_off", "message": "Linking is not available yet on this server"})
    except LinkSessionStale:
        raise HTTPException(status_code=401, detail={"code": "insufficient_user_authentication", "message": "The session confirming this link needs a sign-in within the last 10 minutes"})
    except InvalidGrant:
        raise HTTPException(status_code=401, detail="Invalid or expired link code")
    except LinkRefused as refused:
        raise HTTPException(status_code=409, detail={"code": refused.reason, "message": "This login cannot be added to the account"})
    response.headers["Cache-Control"] = "no-store"
    return identity_read(identity)


@router.post("/token", deprecated=True, response_model=HubTokenResponse, responses={401: {"description": "Invalid client or sign-in code"}, 503: {"description": "Hub sign-in unavailable"}})
def exchange_hub_code(
    body: HubCodeExchangeRequest,
    request: Request,
    response: Response,
    client_id: str = Depends(require_client),
    engine=Depends(get_engine),
) -> HubTokenResponse:
    """Deprecated: use POST /oauth/token with grant_type=authorization_code, which this calls. Kept for Hub releases before 1.8.0."""
    pair = _through_server(request, "/auth/hub/token", [("grant_type", "authorization_code"), ("code", body.code), ("code_verifier", body.code_verifier), ("redirect_uri", body.redirect_uri)], engine)
    if pair is None:
        raise HTTPException(status_code=401, detail="Invalid or expired sign-in code")
    return _token_response(pair, response)


@router.post("/refresh", deprecated=True, response_model=HubTokenResponse, responses={401: {"description": "Invalid client or refresh token"}, 503: {"description": "Hub sign-in unavailable"}})
def refresh_hub_credential(
    body: HubRefreshRequest,
    request: Request,
    response: Response,
    client_id: str = Depends(require_client),
    engine=Depends(get_engine),
) -> HubTokenResponse:
    """Deprecated: use POST /oauth/token with grant_type=refresh_token, which this calls. Kept for Hub releases before 1.8.0."""
    pair = _through_server(request, "/auth/hub/refresh", [("grant_type", "refresh_token"), ("refresh_token", body.refresh_token)], engine)
    if pair is None:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    return _token_response(pair, response)


@router.post(
    "/revoke",
    deprecated=True,
    status_code=204,
    description="Requires HTTP Basic Hub client authentication and JSON containing token. Revokes the entire login token family, including all access and refresh generations. A previously rotated refresh token can revoke the family. Repeated requests, unknown tokens, and tokens belonging to another client return 204 without disclosing token validity. Subsequent authentication checks reject revoked tokens; already-authorized requests may finish. Other login sessions, legacy API keys, and the ORCID session are unaffected. The Hub must also delete its own session and browser cookie.",
    responses={204: {"description": "Revocation processed; no-store response"}, 401: {"description": "Invalid client"}, 503: {"description": "Hub sign-in unavailable"}},
)
def revoke_hub_credential(
    body: HubRevokeRequest,
    request: Request,
    client_id: str = Depends(require_client),
    engine=Depends(get_engine),
) -> Response:
    """Deprecated: use POST /oauth/revoke, which this calls. Kept for Hub releases before 1.8.0."""
    authorization_server().create_endpoint_response("revocation", _server_request(request, "/auth/hub/revoke", [("token", body.token)], engine))
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


password_router = APIRouter(prefix="/oauth", tags=["auth"])
DEVICE_COOKIE = "crucible_device"
SIGN_IN_FAILED = "Sign-in failed. Check your details, or try again in a few minutes."


def _password_path() -> str:
    return f"{get_api_root_path()}/oauth/password"


def _origin(uri: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(uri)
    return f"{parts.scheme}://{parts.netloc}"


def _other_methods(transaction: LoginTransaction, max_age: int | None) -> list[dict]:
    """The providers a person can choose in place of the password, each restarting this sign-in at
    the authorization endpoint with the client's own request."""
    request = {
        "response_type": "code", "client_id": transaction.client_id, "redirect_uri": transaction.callback_uri, "state": transaction.hub_state,
        "code_challenge": transaction.code_challenge, "code_challenge_method": "S256",
    } | ({"max_age": max_age} if max_age is not None else {}) | ({"required_user": transaction.required_user} if transaction.required_user else {})
    return [
        {"display_name": provider.display_name, "is_orcid": provider.is_orcid, "href": f"{public_api_url()}/oauth/authorize?{urlencode({**request, 'provider': provider.key})}"}
        for provider in get_settings().auth.providers
    ]


def _password_page(request: Request, *, state: str = "", username: str = "", problem: str | None = None, expired: bool = False, transaction: LoginTransaction | None = None, max_age: int | None = None, status: int = 200) -> Response:
    """The form posts to itself, then the answer redirects to the client, so form-action names both."""
    from app.auth.password_registration import registration_open

    style_nonce, script_nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    callback_uri = transaction.callback_uri if transaction is not None else None
    response = _templates.TemplateResponse(request, "password_form.html", {
        "style_nonce": style_nonce, "script_nonce": script_nonce, "state": state, "username": username, "problem": problem, "expired": expired,
        "can_register": registration_open(), "max_age": max_age, "methods": _other_methods(transaction, max_age) if transaction is not None else [],
    }, status_code=status)
    targets = "'self'" + (f" {_origin(callback_uri)}" if callback_uri else "")
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{style_nonce}'; script-src 'nonce-{script_nonce}'; form-action {targets}; base-uri 'none'; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


def _password_holder(engine, username: str) -> tuple[int, str, bool] | None:
    """The account a username names, if it may sign in with a password: its id, unique_id and
    whether it waits for approval. Usernames match regardless of case and surrounding spaces."""
    from sqlalchemy import func

    from app.tables import User

    wanted = username.strip().lower()
    if not wanted:
        return None
    with Session(engine) as session:
        user = session.exec(select(User).where(func.lower(User.username) == wanted)).first()
    if user is None or user.is_service_account or user.disabled_at is not None:
        return None
    return user.id, user.unique_id, user.pending_at is not None


def _require_passwords() -> None:
    if not get_settings().auth.password_enabled:
        raise HTTPException(status_code=404, detail="Not found")


@password_router.get("/password", response_class=HTMLResponse, responses={400: {"description": "Unknown, used or expired sign-in", "content": {"text/html": {}}}, 404: {"description": "Passwords are off"}})
def password_form(request: Request, state: str = Query(min_length=1, max_length=128), max_age: int | None = Query(default=None, ge=0, le=86400), engine=Depends(get_engine)) -> Response:
    """The password form for a sign-in started at /oauth/authorize, with the listed providers offered below it."""
    _require_passwords()
    try:
        transaction = peek_login(engine, state, request.cookies.get(login_cookie_name(state)))
    except InvalidLoginRequest:
        return _password_page(request, expired=True, status=400)
    return _password_page(request, state=state, transaction=transaction, max_age=max_age)


@password_router.post("/password", response_class=RedirectResponse, status_code=303, responses={
    303: {"description": "Back to the client with code, state and iss"}, 400: {"description": "Unknown, used or expired sign-in", "content": {"text/html": {}}},
    401: {"description": "Sign-in failed; the same answer for every reason", "content": {"text/html": {}}}, 403: {"description": "The account waits for approval", "content": {"text/html": {}}}, 404: {"description": "Passwords are off"},
})
async def password_sign_in(
    request: Request,
    username: str = Form(max_length=255),
    password: str = Form(max_length=1024),
    state: str = Form(min_length=1, max_length=128),
    max_age: int | None = Form(default=None, ge=0, le=86400),
    engine=Depends(get_engine),
) -> Response:
    """Check a password within the attempt limits. Every failure, for whatever reason, gives the same
    page, status and time. A typo keeps the sign-in; only a success uses it up."""
    from app.auth.throttle import HashBusy, attempt_password, client_address, issue_device_cookie, read_device_cookie

    _require_passwords()
    browser_secret = request.cookies.get(login_cookie_name(state))
    try:
        transaction = await run_in_threadpool(peek_login, engine, state, browser_secret)
    except InvalidLoginRequest:
        return _password_page(request, expired=True, status=400)
    holder = await run_in_threadpool(_password_holder, engine, username)
    user_id = holder[0] if holder else None
    device = await run_in_threadpool(read_device_cookie, request.cookies.get(DEVICE_COOKIE), user_id) if user_id is not None else None
    try:
        valid = await run_in_threadpool(attempt_password, engine, user_id, password, client_address(request), device_cookie=device)
    except HashBusy:
        valid = False
    if not valid:
        return _password_page(request, state=state, username=username.strip(), problem=SIGN_IN_FAILED, transaction=transaction, max_age=max_age, status=401)
    _, unique_id, pending = holder
    if pending:
        await run_in_threadpool(consume_login, engine, state, browser_secret)
        return pending_page(request)
    if transaction.required_user is not None and transaction.required_user != unique_id:
        await run_in_threadpool(consume_login, engine, state, browser_secret)
        response = _browser_headers(RedirectResponse(error_destination(transaction.callback_uri, transaction.hub_state, "login_required"), status_code=303))
    else:
        try:
            destination = await run_in_threadpool(create_local_code, engine, state, browser_secret, user_id)
        except InvalidLoginRequest:
            return _password_page(request, expired=True, status=400)
        response = _deliver(request, transaction.client_id, destination, transaction.callback_uri, transaction.hub_state, status=303)
    secure = request.url.scheme == "https" or public_api_url().startswith("https://")
    changed = await run_in_threadpool(_password_changed_at, engine, user_id)
    device_cookie = await run_in_threadpool(issue_device_cookie, user_id, changed)
    response.set_cookie(DEVICE_COOKIE, device_cookie, max_age=get_settings().auth.device_cookie_days * 86400, path=_password_path(), secure=secure, httponly=True, samesite="lax")
    response.delete_cookie(login_cookie_name(state), path=_password_path())
    return response


def _password_changed_at(engine, user_id: int):
    from app.tables import LocalCredential

    with Session(engine) as session:
        return session.get(LocalCredential, user_id).password_changed_at
