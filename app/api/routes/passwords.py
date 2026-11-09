"""Password links: an administrator issues one, and the person opens it in a browser to set or
reset their password. On first open the token moves into a cookie bound to this browser and the
address loses it, so it does not stay in the browser's history."""
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlmodel import Session

from app.api.dependencies import get_api_root_path, get_engine
from app.api.path_params import UserRef
from app.api.routes.hub_auth import _browser_headers, _require_passwords, _templates
from app.auth.password_links import LinkRefused, issue_link, link_purpose, live_link, use_link
from app.auth.passwords import MAX_LENGTH, check_new_password
from app.auth.throttle import HashBusy, begin_code_attempt, client_address, give_back, hash_slot
from app.config import get_secret, get_settings
from app.models.identities import PasswordLinkRead
from app.utils.auth_utils import authorize_user, caller_row, require_human_admin_safeguard, require_platform_admin, require_recent_authentication
from app.utils.hub_login import HubLoginUnavailable, public_api_url
from app.utils.permissions import Principal
from app.utils.user_utils import resolve_user_by_identifier

router = APIRouter(tags=["users"])
pages = APIRouter(prefix="/oauth", tags=["auth"])
COOKIE = "crucible_password_link"
BROWSER_LIFETIME = 900
NOT_VALID = "This link is not valid, was already used or has expired. Ask an administrator for a new one."


def link_url(base: str, token: str) -> str:
    return f"{base}/oauth/password-link?token={token}"


@router.post("/users/{user_ref}/password-link", response_model=PasswordLinkRead, status_code=201, responses={
    401: {"description": "A session needs a recent sign-in (`insufficient_user_authentication`)"},
    403: {"description": "Not a platform administrator; `hub_session_required` for any human administrator, yourself included; `browser_sign_in_required` for a session approved with a device code"},
    404: {"description": "No such person"},
    409: {"description": "`passwords_off`, `service_account`, `account_disabled` or `account_pending`"},
    503: {"description": "The public API address is not set"},
})
def issue_password_link(user_ref: UserRef, response: Response, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """A one-time link for the person to set a password, or choose a new one, shown only here.
    It replaces any earlier link, works once, and lasts CRUCIBLE_AUTH_PASSWORD_LINK_HOURS. Using it
    ends the person's sessions and does not sign them in. Platform administrators only."""
    require_platform_admin(auth_res)
    require_recent_authentication(auth_res)
    if not get_settings().auth.password_enabled:
        raise HTTPException(status_code=409, detail={"code": "passwords_off", "message": "Password sign-in is off on this server"})
    try:
        base = public_api_url()
    except HubLoginUnavailable:
        raise HTTPException(status_code=503, detail="The public API address is not set") from None
    with Session(get_engine()) as session, session.begin():
        user = resolve_user_by_identifier(session, user_ref)
        require_human_admin_safeguard(auth_res, user)
        purpose = link_purpose(session, user)
        try:
            token, expires_at = issue_link(session, user, purpose, actor=caller_row(session, auth_res))
        except LinkRefused as refused:
            raise HTTPException(status_code=409, detail={"code": refused.code, "message": refused.message}) from None
    response.headers["Cache-Control"] = "no-store"
    return PasswordLinkRead(url=link_url(base, token), purpose=purpose, expires_at=expires_at)


def _serializer(purpose: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_secret("CRUCIBLE_SECRET_KEY"), salt=f"crucible.password-link.{purpose}")


def _cookie_path() -> str:
    return f"{get_api_root_path()}/oauth/password-link"


def _page(request: Request, template: str, context: dict, status: int = 200, *, form: bool = False) -> Response:
    style_nonce, script_nonce = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    response = _templates.TemplateResponse(request, template, {**context, "style_nonce": style_nonce, "script_nonce": script_nonce}, status_code=status)
    scripts = f"; script-src 'nonce-{script_nonce}'" if form else ""
    targets = "'self'" if form else "'none'"
    response.headers["Content-Security-Policy"] = f"default-src 'none'; style-src 'nonce-{style_nonce}'{scripts}; base-uri 'none'; form-action {targets}; frame-ancestors 'none'"
    response.headers["X-Frame-Options"] = "DENY"
    return _browser_headers(response)


def _done(request: Request, heading: str, message: str, status: int = 200) -> Response:
    response = _page(request, "device_done.html", {"heading": heading, "message": message}, status)
    response.delete_cookie(COOKIE, path=_cookie_path())
    return response


def _form(request: Request, link, nonce: str, *, problem: str | None = None, status: int = 200) -> Response:
    return _page(request, "password_link_form.html", {
        "setup": link.purpose == "setup", "person": link.person, "username": link.username, "problem": problem,
        "min_length": get_settings().auth.password_min_length, "max_length": MAX_LENGTH,
        "form_token": _serializer("form").dumps({"n": nonce}),
    }, status, form=True)


def _browser(request: Request) -> dict | None:
    try:
        data = _serializer("cookie").loads(request.cookies.get(COOKIE, ""), max_age=BROWSER_LIFETIME)
    except BadData:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("t"), str) and isinstance(data.get("n"), str) else None


@pages.get("/password-link", response_class=HTMLResponse, responses={
    303: {"description": "The token moved into a cookie; the same page without it"}, 400: {"description": "Unknown, used or expired link", "content": {"text/html": {}}},
    404: {"description": "Passwords are off"},
})
async def password_link_page(request: Request, token: str | None = Query(default=None, max_length=128), engine=Depends(get_engine)) -> Response:
    """Where a password link leads. Opening it does not use it up, so a mail scanner that follows
    the link changes nothing."""
    _require_passwords()
    if token is None:
        browser = _browser(request)
        link = await run_in_threadpool(live_link, engine, browser["t"]) if browser else None
        if link is None:
            return _done(request, "Link not valid", NOT_VALID, 400)
        return _form(request, link, browser["n"])
    allowed, digest = await run_in_threadpool(begin_code_attempt, engine, client_address(request))
    link = await run_in_threadpool(live_link, engine, token) if allowed else None
    if link is None:
        return _done(request, "Link not valid", NOT_VALID, 400)
    await run_in_threadpool(give_back, engine, digest)
    try:
        base = public_api_url()
    except HubLoginUnavailable:
        return _done(request, "Unavailable", "Passwords cannot be set on this server right now.", 503)
    response = _browser_headers(RedirectResponse(f"{base}/oauth/password-link", status_code=303))
    response.set_cookie(COOKIE, _serializer("cookie").dumps({"t": token, "n": secrets.token_urlsafe(24)}), max_age=BROWSER_LIFETIME, path=_cookie_path(),
                        secure=base.startswith("https://"), httponly=True, samesite="lax")
    return response


def _set_password(engine, token: str, password: str) -> None:
    with hash_slot(), Session(engine) as session, session.begin():
        use_link(session, token, password)


@pages.post("/password-link", response_class=HTMLResponse, responses={
    400: {"description": "Unknown, used or expired link, or a form from another browser", "content": {"text/html": {}}},
    404: {"description": "Passwords are off"}, 422: {"description": "The passwords differ or break a rule", "content": {"text/html": {}}},
    503: {"description": "The server is busy checking passwords", "content": {"text/html": {}}},
})
async def password_link_submitted(
    request: Request,
    password: str = Form(max_length=1024),
    confirm: str = Form(max_length=1024),
    form_token: str = Form(max_length=2048),
    engine=Depends(get_engine),
) -> Response:
    """Set the password and use up the link. Two tabs cannot both succeed: the link's row is locked."""
    _require_passwords()
    browser = _browser(request)
    try:
        form = _serializer("form").loads(form_token, max_age=BROWSER_LIFETIME)
    except BadData:
        form = None
    if browser is None or not isinstance(form, dict) or not secrets.compare_digest(str(form.get("n", "")), browser["n"]):
        return _done(request, "Not saved", "This form did not come from this browser, or it waited too long. Open the link again.", 400)
    allowed, digest = await run_in_threadpool(begin_code_attempt, engine, client_address(request))
    link = await run_in_threadpool(live_link, engine, browser["t"]) if allowed else None
    if link is None:
        return _done(request, "Link not valid", NOT_VALID, 400)
    await run_in_threadpool(give_back, engine, digest)
    if password != confirm:
        return _form(request, link, browser["n"], problem="The two passwords are different. Type the same password in both.", status=422)
    problem = check_new_password(password, context=link.context)
    if problem is not None:
        return _form(request, link, browser["n"], problem=problem.message, status=422)
    try:
        await run_in_threadpool(_set_password, engine, browser["t"], password)
    except HashBusy:
        return _form(request, link, browser["n"], problem="The server is busy. Try again in a moment.", status=503)
    except LinkRefused:
        return _done(request, "Link not valid", NOT_VALID, 400)
    if link.purpose == "setup":
        return _done(request, "Password set", "Your password is set. Sign in with it from the Hub or the program you use.")
    return _done(request, "Password changed", "Your password is changed and you were signed out everywhere. Sign in again with the new one.")
