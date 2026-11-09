"""Passwords set or changed through the API, and password links: an administrator issues one, and
the person opens it in a browser to set or reset their password. On first open the token moves into
a cookie bound to this browser and the address loses it, so it does not stay in the browser's history."""
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadData, URLSafeTimedSerializer
from sqlmodel import Session, select

from app.api.dependencies import get_api_root_path, get_engine
from app.api.path_params import UserRef
from app.api.routes.hub_auth import _browser_headers, _require_passwords, _templates
from app.auth.password_links import LinkRefused, issue_link, link_purpose, live_link, use_link
from app.auth.passwords import MAX_LENGTH, check_new_password
from app.auth.throttle import HashBusy, begin_code_attempt, client_address, give_back, hash_slot
from app.api.errors import feature_disabled_response
from app.config import FeatureDisabled, get_secret, get_settings
from app.models.identities import PasswordChange, PasswordLinkRead
from app.utils.auth_utils import authorize_user, caller_row, require_credential_change, require_human_admin_safeguard, require_platform_admin, require_recent_authentication
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
    409: {"description": "`service_account`, `account_disabled` or `account_pending`"},
    **feature_disabled_response("passwords"),
    503: {"description": "The public API address is not set"},
})
def issue_password_link(user_ref: UserRef, response: Response, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """A one-time link for the person to set a password, or choose a new one, shown only here.
    It replaces any earlier link, works once, and lasts CRUCIBLE_AUTH_PASSWORD_LINK_HOURS. Using it
    ends the person's sessions and does not sign them in. Platform administrators only."""
    require_platform_admin(auth_res)
    require_recent_authentication(auth_res)
    _passwords_on()
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


class _PasswordChanged(Exception):
    """Someone set or removed the password between the check and the write."""


def _passwords_on() -> None:
    if not get_settings().auth.password_enabled:
        raise FeatureDisabled("passwords")


def _field_error(field: str, kind: str, message: str, **context) -> HTTPException:
    """A 422 in FastAPI's validation shape, so clients show it on the field."""
    error = {"type": kind, "loc": ["body", field], "msg": message}
    if context:
        error["ctx"] = context
    return HTTPException(status_code=422, detail=[error])


def _caller_and_credential(engine, auth_res: Principal):
    from app.models.core_models import LocalCredential

    with Session(engine) as session:
        user = caller_row(session, auth_res)
        if user is None:
            raise HTTPException(status_code=404, detail="User not found")
        require_credential_change(auth_res, target=user)
        if user.is_service_account:
            raise HTTPException(status_code=409, detail={"code": "service_account", "message": "Service accounts sign in with API keys, not passwords"})
        session.expunge(user)
        return user, session.get(LocalCredential, user.id) is not None


def _write_password(engine, user_id: int, password: str, *, first: bool, keep_family: str | None) -> None:
    from app.auth.events import AuthEventType, record_event
    from app.auth.passwords import store_password
    from app.models.core_models import LocalCredential, User
    from app.utils.hub_tokens import revoke_all_hub_tokens

    with hash_slot(), Session(engine) as session, session.begin():
        user = session.exec(select(User).where(User.id == user_id).with_for_update()).one()
        if (session.get(LocalCredential, user.id) is None) != first or not user.username:
            raise _PasswordChanged
        store_password(session, user, password)
        if not first:
            revoke_all_hub_tokens(session, user.id, except_family_id=keep_family)
        record_event(session, AuthEventType.PASSWORD_SET if first else AuthEventType.PASSWORD_CHANGED, user=user, actor=user)


@router.post("/account/password", status_code=204, tags=["account"], responses={
    401: {"description": "A session needs a recent sign-in (`insufficient_user_authentication`)"},
    403: {"description": "`hub_session_required` for an API key, `browser_sign_in_required` for a session approved with a device code"},
    409: {"description": "`service_account`, `username_required` (set one with PATCH /account/profile first), or `password_changed` when another request changed it first"},
    **feature_disabled_response("passwords"),
    422: {"description": "Field errors in FastAPI's shape: `current_password_required` or `current_password_invalid` (counted against the account's attempt limit) on `current_password`, `password_rejected` with `ctx.reason` on `new_password`"},
    503: {"description": "The server is busy checking passwords"},
})
async def set_account_password(body: PasswordChange, request: Request, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Set a first password, or change yours, from a browser session with a recent sign-in. Changing
    needs the current password and ends your other sessions, keeping this one. Passwords sign in with
    the username, so an account without one sets it first with PATCH /account/profile. API keys are
    never touched."""
    from app.auth.throttle import attempt_password

    _passwords_on()
    engine = get_engine()
    user, has_password = await run_in_threadpool(_caller_and_credential, engine, auth_res)
    if not user.username:
        raise HTTPException(status_code=409, detail={"code": "username_required", "message": "Passwords sign in with a username; set one with PATCH /account/profile first"})
    if has_password:
        if not body.current_password:
            raise _field_error("current_password", "current_password_required", "Give your current password to change it")
        try:
            valid = await run_in_threadpool(attempt_password, engine, user.id, body.current_password, client_address(request), session_authenticated=True)
        except HashBusy:
            raise HTTPException(status_code=503, detail="The server is busy checking passwords; try again in a moment") from None
        if not valid:
            raise _field_error("current_password", "current_password_invalid", "The current password is not correct")
    problem = check_new_password(body.new_password, context=(user.username, user.email, user.first_name, user.last_name))
    if problem is not None:
        raise _field_error("new_password", "password_rejected", problem.message, reason=problem.code)
    try:
        await run_in_threadpool(lambda: _write_password(engine, user.id, body.new_password, first=not has_password, keep_family=auth_res.session_family_id))
    except HashBusy:
        raise HTTPException(status_code=503, detail="The server is busy checking passwords; try again in a moment") from None
    except _PasswordChanged:
        raise HTTPException(status_code=409, detail={"code": "password_changed", "message": "The password or username was changed by another request; check and try again"}) from None
    return Response(status_code=204)


@router.delete("/users/{user_ref}/password", status_code=204, responses={
    401: {"description": "A session needs a recent sign-in (`insufficient_user_authentication`)"},
    403: {"description": "Not a platform administrator; `hub_session_required` for any human administrator, yourself included; `browser_sign_in_required` for a session approved with a device code"},
    404: {"description": "No such person, or `no_password`"},
    409: {"description": "`service_account`, or `last_sign_in_method` when removing your own only way in"},
})
def remove_user_password(user_ref: UserRef, auth_res: Annotated[Principal, Depends(authorize_user)]):
    """Remove a person's password, for example after a compromise: it stops working at once, their
    sessions end and any password link is cancelled. Issue a new link afterwards with
    POST /users/{user_ref}/password-link. API keys are not touched; revoke them separately.
    Platform administrators, or yourself if you have another way to sign in."""
    from app.auth.events import AuthEventType, record_event
    from app.auth.linking import sign_in_methods
    from app.auth.password_links import void_links
    from app.models.core_models import LocalCredential, User
    from app.utils.hub_tokens import revoke_all_hub_tokens

    with Session(get_engine()) as session, session.begin():
        user = resolve_user_by_identifier(session, user_ref)
        require_credential_change(auth_res, target=user)
        if user.is_service_account:
            raise HTTPException(status_code=409, detail={"code": "service_account", "message": "Service accounts have no password"})
        session.exec(select(User.id).where(User.id == user.id).with_for_update()).one()
        credential = session.get(LocalCredential, user.id)
        if credential is None:
            raise HTTPException(status_code=404, detail={"code": "no_password", "message": "This person has no password"})
        if auth_res.is_user(user) and not sign_in_methods(session, user):
            raise HTTPException(status_code=409, detail={"code": "last_sign_in_method", "message": "This is your only way to sign in; link another login first"})
        session.delete(credential)
        void_links(session, user)
        revoke_all_hub_tokens(session, user.id)
        record_event(session, AuthEventType.PASSWORD_REMOVED, user=user, actor=caller_row(session, auth_res))
    return Response(status_code=204)


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


REGISTER_COOKIE = "crucible_register"


def _register_cookie_path() -> str:
    return f"{get_api_root_path()}/oauth/register"


def _secure_cookies() -> bool:
    try:
        return public_api_url().startswith("https://")
    except HubLoginUnavailable:
        return False


def _register_page(request: Request, nonce: str, values: dict | None = None, *, problem: str | None = None, status: int = 200) -> Response:
    fields = {"first_name": "", "last_name": "", "email": "", "username": "", **(values or {})}
    return _page(request, "password_register_form.html", {
        **fields, "problem": problem, "min_length": get_settings().auth.password_min_length, "max_length": MAX_LENGTH,
        "form_token": _serializer("register-form").dumps({"n": nonce}),
    }, status, form=True)


def _register_browser(request: Request) -> str | None:
    try:
        data = _serializer("register-cookie").loads(request.cookies.get(REGISTER_COOKIE, ""), max_age=BROWSER_LIFETIME)
    except BadData:
        return None
    return data.get("n") if isinstance(data, dict) and isinstance(data.get("n"), str) else None


def _require_registration() -> None:
    from app.auth.password_registration import registration_open

    if not registration_open():
        raise HTTPException(status_code=404, detail="Not found")


@pages.get("/register", response_class=HTMLResponse, responses={404: {"description": "Password registration is off"}})
def registration_form(request: Request) -> Response:
    """Where someone asks for an account with a password, when the server allows it
    (CRUCIBLE_AUTH_PASSWORD_REGISTRATION=approval). An administrator approves each one."""
    _require_registration()
    nonce = secrets.token_urlsafe(24)
    response = _register_page(request, nonce)
    response.set_cookie(REGISTER_COOKIE, _serializer("register-cookie").dumps({"n": nonce}), max_age=BROWSER_LIFETIME, path=_register_cookie_path(),
                        secure=_secure_cookies(), httponly=True, samesite="lax")
    return response


def _create_account(engine, fields: tuple[str, str, str, str], password: str) -> None:
    from sqlalchemy.exc import IntegrityError

    from app.auth.password_registration import IN_USE, RegistrationProblem, RegistrationRefused, register

    username, first_name, last_name, email = fields
    with hash_slot(), Session(engine) as session:
        try:
            with session.begin():
                register(session, username=username, first_name=first_name, last_name=last_name, email=email, password=password)
        except IntegrityError:
            raise RegistrationRefused(RegistrationProblem("username", IN_USE, 409)) from None


def _checked(engine, **fields) -> tuple[str, str, str, str]:
    from app.auth.password_registration import checked_fields

    with Session(engine) as session:
        return checked_fields(session, **fields)


@pages.post("/register", response_class=HTMLResponse, responses={
    400: {"description": "A form from another browser, or one that waited too long", "content": {"text/html": {}}},
    404: {"description": "Password registration is off"},
    409: {"description": "The username or email is already in use; one answer for both", "content": {"text/html": {}}},
    422: {"description": "A field breaks a rule, or the passwords differ", "content": {"text/html": {}}},
    429: {"description": "Too many requests from this address; wait a minute", "content": {"text/html": {}}},
    503: {"description": "The server is busy checking passwords", "content": {"text/html": {}}},
})
async def registration_submitted(
    request: Request,
    first_name: str = Form(max_length=100),
    last_name: str = Form(max_length=100),
    email: str = Form(max_length=254),
    username: str = Form(max_length=64),
    password: str = Form(max_length=1024),
    confirm: str = Form(max_length=1024),
    form_token: str = Form(max_length=2048),
    engine=Depends(get_engine),
) -> Response:
    """Create an account that waits for an administrator's approval. Every submission counts against
    the address's limit, so one source cannot fill the approval list. It signs nobody in."""
    from app.auth.password_registration import RegistrationRefused

    _require_registration()
    nonce = _register_browser(request)
    try:
        form = _serializer("register-form").loads(form_token, max_age=BROWSER_LIFETIME)
    except BadData:
        form = None
    if nonce is None or not isinstance(form, dict) or not secrets.compare_digest(str(form.get("n", "")), nonce):
        return _done(request, "Not sent", "This form did not come from this browser, or it waited too long. Open the page again.", 400)
    values = {"first_name": first_name.strip(), "last_name": last_name.strip(), "email": email.strip(), "username": username.strip()}
    allowed, _ = await run_in_threadpool(begin_code_attempt, engine, client_address(request))
    if not allowed:
        return _register_page(request, nonce, values, problem="Too many requests from your network. Wait a minute and try again.", status=429)
    if password != confirm:
        return _register_page(request, nonce, values, problem="The two passwords are different. Type the same password in both.", status=422)
    try:
        fields = await run_in_threadpool(lambda: _checked(engine, username=username, first_name=first_name, last_name=last_name, email=email))
    except RegistrationRefused as refused:
        return _register_page(request, nonce, values, problem=refused.problem.message, status=refused.problem.status)
    problem = check_new_password(password, context=fields)
    if problem is not None:
        return _register_page(request, nonce, values, problem=problem.message, status=422)
    try:
        await run_in_threadpool(_create_account, engine, fields, password)
    except HashBusy:
        return _register_page(request, nonce, values, problem="The server is busy. Try again in a moment.", status=503)
    except RegistrationRefused as refused:
        return _register_page(request, nonce, values, problem=refused.problem.message, status=refused.problem.status)
    response = _done(request, "Request sent", "Thank you. An administrator will review your request. Once it is approved, sign in with your username and password.")
    response.delete_cookie(REGISTER_COOKIE, path=_register_cookie_path())
    return response
