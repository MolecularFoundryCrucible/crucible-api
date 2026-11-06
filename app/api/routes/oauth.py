from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from app.api.dependencies import get_engine
from app.auth.clients import ClientUnavailable
from app.auth.oauth_server import FormRequest, authorization_server
from app.utils.hub_login import HubLoginUnavailable, public_api_url

router = APIRouter(prefix="/oauth", tags=["auth"])

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
