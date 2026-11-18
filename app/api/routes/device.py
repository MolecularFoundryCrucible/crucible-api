"""The browser side of device sign-in (RFC 8628 3.3): enter the code, sign in afresh, approve.

The page signs the person in through the API's own sign-in, as an internal client with PKCE, so
every provider, passwords, pending accounts and refusals behave exactly as anywhere else. A cookie
bound to this browser carries the PKCE verifier and a nonce that the sign-in state and the decision
must both match."""
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from authlib.oauth2.rfc7636 import create_s256_code_challenge
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlmodel import Session

from app.api.dependencies import get_api_root_path, get_engine
from app.api.routes.browser_sign_in import _browser_headers, _templates
from app.auth.clients import client_by_id
from app.auth.devices import INTERNAL_CLIENT_ID, decide, device_by_hash, pending_device, record_signed_in
from app.auth.throttle import begin_code_attempt, client_address, give_back
from app.config import get_secret, get_settings
from app.tables import User
from app.auth.login_transactions import SignInUnavailable, public_api_url, redeem_approval_code
from app.auth.tokens import InvalidGrant

router = APIRouter(prefix="/oauth/device", tags=["auth"])
COOKIE = "crucible_device_approval"
FRESH_SIGN_IN = timedelta(minutes=10)
NOT_VALID = "That code is not valid or has expired. Check it, or start again on your device."


def _serializer(key: str, purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(key, salt=f"crucible.device-approval.{purpose}")


async def _secret_key() -> str:
    return await run_in_threadpool(get_secret, "CRUCIBLE_SECRET_KEY")


def _cookie_path() -> str:
    return f"{get_api_root_path()}/oauth/device"


def _form_targets() -> str:
    """Where submitting the code form may lead. Chrome applies form-action to every redirect after the
    submission, and entering a code redirects through /oauth/authorize to the identity provider, so
    the providers' sites are listed beside this one."""
    origins = []
    for provider in get_settings().auth.providers:
        parts = urlsplit(provider.issuer)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in origins:
            origins.append(origin)
    return " ".join(["'self'", *origins])


def _page(request: Request, template: str, context: dict, status: int = 200, *, form: bool = False) -> Response:
    nonce = secrets.token_urlsafe(16)
    response = _templates.TemplateResponse(request, template, {**context, "style_nonce": nonce}, status_code=status)
    targets = _form_targets() if form else "'none'"
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{nonce}'; base-uri 'none'; form-action {targets}; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


def _done(request: Request, heading: str, message: str, status: int = 200) -> Response:
    response = _page(request, "device_done.html", {"heading": heading, "message": message}, status)
    response.delete_cookie(COOKIE, path=_cookie_path())
    return response


def _browser(request: Request, key: str) -> dict | None:
    try:
        data = _serializer(key, "cookie").loads(request.cookies.get(COOKIE, ""), max_age=900)
    except BadData:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("n"), str) and isinstance(data.get("v"), str) else None


@router.get("", response_class=HTMLResponse)
def device_code_form(request: Request, user_code: str | None = Query(default=None, max_length=64)) -> Response:
    """Where a person enters the code a program on another device shows."""
    return _page(request, "device_code.html", {"user_code": user_code or "", "problem": None}, form=True)


@router.post("", response_class=RedirectResponse, status_code=302, responses={400: {"description": "Unknown or expired code; the same answer for every reason", "content": {"text/html": {}}}})
async def device_code_entered(request: Request, user_code: str = Form(max_length=64), engine=Depends(get_engine)) -> Response:
    """Check the code within the per-address attempt limit, then sign the person in afresh."""
    key = await _secret_key()
    allowed, digest = await run_in_threadpool(begin_code_attempt, engine, client_address(request))
    device = await run_in_threadpool(pending_device, engine, user_code) if allowed else None
    if device is None:
        return _page(request, "device_code.html", {"user_code": user_code[:32], "problem": NOT_VALID}, 400, form=True)
    await run_in_threadpool(give_back, engine, digest)
    try:
        base = public_api_url()
    except SignInUnavailable:
        return _done(request, "Sign-in is unavailable", "Device sign-in is not available on this server.", 503)
    nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    challenge = create_s256_code_challenge(verifier)
    state = _serializer(key, "state").dumps({"d": device.device_hash, "n": nonce, "c": device.user_code})
    destination = f"{base}/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": INTERNAL_CLIENT_ID, "redirect_uri": f"{base}/oauth/device/callback", "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256", "max_age": "0",
    })
    response = _browser_headers(RedirectResponse(destination, status_code=303))
    response.set_cookie(COOKIE, _serializer(key, "cookie").dumps({"n": nonce, "v": verifier}), max_age=900, path=_cookie_path(), secure=base.startswith("https://"), httponly=True, samesite="lax")
    return response


def _person_name(engine, user_id: int) -> str:
    with Session(engine) as session:
        user = session.get(User, user_id)
        return " ".join(part for part in (user.first_name, user.last_name) if part) or user.username or user.unique_id


@router.get("/callback", response_class=HTMLResponse, responses={400: {"description": "The sign-in cannot be matched to this browser, or it failed", "content": {"text/html": {}}}})
async def device_signed_in(
    request: Request,
    state: str = Query(default="", max_length=2048),
    code: str | None = Query(default=None, max_length=128),
    error: str | None = Query(default=None, max_length=64),
    iss: str | None = Query(default=None, max_length=512),
    engine=Depends(get_engine),
) -> Response:
    """After the fresh sign-in: show what is being approved, for whom."""
    key = await _secret_key()
    browser = _browser(request, key)
    try:
        started = _serializer(key, "state").loads(state, max_age=900)
    except BadData:
        started = None
    if browser is None or not isinstance(started, dict) or not secrets.compare_digest(str(started.get("n", "")), browser["n"]):
        return _done(request, "Sign-in failed", "This sign-in did not start in this browser. Start again on your device.", 400)
    try:
        base = public_api_url()
    except SignInUnavailable:
        return _done(request, "Sign-in is unavailable", "Device sign-in is not available on this server.", 503)
    if iss != base:
        return _done(request, "Sign-in failed", "The answer did not come from this server. Start again on your device.", 400)
    if error is not None or code is None:
        return _done(request, "Not approved", "The sign-in was cancelled or refused, so the device was not approved.", 400)
    try:
        user_id, auth_time = await run_in_threadpool(redeem_approval_code, engine, code, browser["v"], INTERNAL_CLIENT_ID, f"{base}/oauth/device/callback")
    except InvalidGrant:
        return _done(request, "Sign-in failed", "This sign-in expired or was already used. Start again on your device.", 400)
    if auth_time is None or datetime.now(timezone.utc) - auth_time > FRESH_SIGN_IN:
        return _done(request, "Sign-in failed", "Approving a device needs a sign-in just now. Start again on your device.", 400)
    device = await run_in_threadpool(device_by_hash, engine, started["d"])
    if device is None or not await run_in_threadpool(lambda: record_signed_in(engine, device.device_hash, user_id=user_id, auth_time=auth_time)):
        return _done(request, "Code expired", "The code expired or was already used. Start again on your device.", 400)
    person = await run_in_threadpool(_person_name, engine, user_id)
    client = client_by_id(device.client_id)
    token = _serializer(key, "decision").dumps({"d": device.device_hash, "u": user_id, "n": browser["n"]})
    return _page(request, "device_confirm.html", {
        "client_name": client.display_name if client else device.client_id, "person": person, "asked_at": device.created_at.strftime("%H:%M UTC"),
        "user_code": started.get("c", ""), "token": token, "requested_from": device.requested_from,
    }, form=True)


@router.post("/approve", response_class=HTMLResponse, responses={400: {"description": "The decision cannot be matched to this browser, or it came too late", "content": {"text/html": {}}}})
async def device_decided(request: Request, decision_token: str = Form(max_length=2048), decision: str = Form(pattern="^(approve|deny)$"), engine=Depends(get_engine)) -> Response:
    """Record Approve or Deny once. The program learns it at its next poll."""
    key = await _secret_key()
    browser = _browser(request, key)
    try:
        decided = _serializer(key, "decision").loads(decision_token, max_age=300)
    except BadData:
        decided = None
    if browser is None or not isinstance(decided, dict) or not secrets.compare_digest(str(decided.get("n", "")), browser["n"]):
        return _done(request, "Not recorded", "This decision did not come from this browser. Start again on your device.", 400)
    approve = decision == "approve"
    recorded = await run_in_threadpool(lambda: decide(engine, decided["d"], user_id=decided["u"], approve=approve))
    if not recorded:
        return _done(request, "Code expired", "The code expired or was already decided. Start again on your device.", 400)
    if approve:
        return _done(request, "Device approved", "Done. You can go back to your device; it signs in within a few seconds.")
    return _done(request, "Device denied", "The device was not approved. If you did not start it, you can ignore it; nothing was signed in.")
