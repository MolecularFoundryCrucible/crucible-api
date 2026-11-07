from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.api.dependencies import get_engine
from app.auth.clients import ClientUnavailable, sign_in_enabled
from app.auth.oauth_server import FormRequest, authorization_server
from app.utils.hub_login import HubLoginUnavailable, public_api_url

router = APIRouter(prefix="/oauth", tags=["auth"])
discovery = APIRouter(tags=["auth"])

_FORM = {"requestBody": {"content": {"application/x-www-form-urlencoded": {"schema": {"type": "object", "additionalProperties": {"type": "string"}}}}, "required": True}}


def public_uri(path: str) -> str:
    try:
        return f"{public_api_url()}{path}"
    except HubLoginUnavailable:
        raise HTTPException(status_code=503, detail="Sign-in is unavailable") from None


class RepeatedParameter(Exception):
    pass


async def oauth_request(request: Request, path: str, engine) -> FormRequest:
    """RFC 6749 3.2: a parameter without a value counts as omitted, and none may appear twice."""
    form = await request.form()
    fields = [(name, value) for name, value in form.multi_items() if isinstance(value, str) and value]
    names = [name for name, _ in fields]
    if len(names) != len(set(names)):
        raise RepeatedParameter
    headers = {"Authorization": request.headers["authorization"]} if "authorization" in request.headers else {}
    return FormRequest(public_uri(path), headers, fields, engine)


def repeated() -> JSONResponse:
    return JSONResponse({"error": "invalid_request", "error_description": "A parameter is repeated"}, status_code=400, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


def answer(status: int, body: dict, headers: dict) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=headers)


@router.post("/token", openapi_extra=_FORM, responses={200: {"description": "Tokens: access_token, token_type Bearer, expires_in, refresh_token, and auth_time when known", "content": {"application/json": {"schema": {"type": "object", "properties": {"access_token": {"type": "string"}, "token_type": {"type": "string"}, "expires_in": {"type": "integer"}, "refresh_token": {"type": "string"}, "auth_time": {"type": "integer"}}}}}}, 400: {"description": "RFC 6749 error, such as `invalid_grant` or `invalid_request`"}, 401: {"description": "`invalid_client`"}, 503: {"description": "Sign-in unavailable"}})
async def token(request: Request, engine=Depends(get_engine)) -> JSONResponse:
    """The OAuth 2.0 token endpoint (RFC 6749): `grant_type=authorization_code` with `code`,
    `redirect_uri` and the PKCE `code_verifier`, or `grant_type=refresh_token`. Clients authenticate
    with HTTP Basic. Refresh tokens rotate, and presenting a used one ends the whole session."""
    try:
        prepared = await oauth_request(request, "/oauth/token", engine)
    except RepeatedParameter:
        return repeated()
    try:
        return answer(*await run_in_threadpool(authorization_server().create_token_response, prepared))
    except ClientUnavailable:
        raise HTTPException(status_code=503, detail="Sign-in is unavailable") from None


@router.post("/revoke", openapi_extra=_FORM, responses={200: {"description": "Processed; unknown tokens and other clients' tokens are treated alike", "content": {"application/json": {"schema": {"type": "object"}}}}, 401: {"description": "`invalid_client`"}, 503: {"description": "Sign-in unavailable"}})
async def revoke(request: Request, engine=Depends(get_engine)) -> JSONResponse:
    """Token revocation (RFC 7009): `token`, and optionally `token_type_hint`. Ends the whole session
    the token belongs to. Clients authenticate with HTTP Basic."""
    try:
        prepared = await oauth_request(request, "/oauth/revoke", engine)
    except RepeatedParameter:
        return repeated()
    try:
        return answer(*await run_in_threadpool(authorization_server().create_endpoint_response, "revocation", prepared))
    except ClientUnavailable:
        raise HTTPException(status_code=503, detail="Sign-in is unavailable") from None


@router.get("/authorize", response_class=RedirectResponse, status_code=302, responses={200: {"description": "A page to choose how to sign in, when several providers are listed and none is named", "content": {"text/html": {}}}, 302: {"description": "To the provider, or back to the client with `error`, `state` and `iss`"}, 400: {"description": "Unknown client or redirect, which are never redirected to"}, 503: {"description": "Sign-in unavailable"}})
async def authorize(
    request: Request,
    response_type: str | None = Query(default=None, max_length=32),
    client_id: str | None = Query(default=None, max_length=100),
    redirect_uri: str | None = Query(default=None, max_length=512),
    state: str | None = Query(default=None, max_length=256, description="Required, 16 to 256 ASCII characters"),
    code_challenge: str | None = Query(default=None, max_length=128, description="Required: PKCE with S256"),
    code_challenge_method: str | None = Query(default=None, max_length=16),
    max_age: int | None = Query(default=None, ge=0, le=86400, description="Maximum seconds since the person last signed in at the provider; 0 asks the provider to sign them in again."),
    provider: str | None = Query(default=None, max_length=32, description="The provider to use, a key from GET /auth/methods. When omitted, the only listed provider is used, or a page offers the choice."),
    required_user: str | None = Query(default=None, min_length=1, max_length=255, description="The unique_id that must sign in; any other account gets error=login_required."),
    engine=Depends(get_engine),
) -> Response:
    """The OAuth 2.0 authorization endpoint (RFC 6749 4.1) for registered clients: `response_type=code`,
    `client_id`, an exact `redirect_uri`, `state`, and PKCE with S256. The answer comes back to the
    redirect with `code` or `error`, `state`, and `iss` (RFC 9207)."""
    from app.api.routes.hub_auth import start_sign_in

    return await start_sign_in(request, engine, path="/oauth/authorize", response_type=response_type, client_id=client_id, redirect_uri=redirect_uri, state=state,
                               code_challenge=code_challenge, code_challenge_method=code_challenge_method, max_age=max_age, provider=provider, required_user=required_user)


@discovery.get("/.well-known/oauth-authorization-server", responses={200: {"description": "Authorization server metadata (RFC 8414)", "content": {"application/json": {"schema": {"type": "object"}}}}, 404: {"description": "No sign-in clients are registered"}})
def authorization_server_metadata() -> JSONResponse:
    """What this API's OAuth server offers and where (RFC 8414). Served under the API address, not
    the host root; `issuer` is the API's public address, the same value as `iss` in redirects."""
    if not sign_in_enabled():
        raise HTTPException(status_code=404, detail="Not found")
    try:
        issuer = public_api_url()
    except HubLoginUnavailable:
        raise HTTPException(status_code=503, detail="Sign-in is unavailable") from None
    return JSONResponse({
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "response_types_supported": ["code"],
        "response_modes_supported": ["query"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
        "revocation_endpoint_auth_methods_supported": ["client_secret_basic", "none"],
        "authorization_response_iss_parameter_supported": True,
    })
