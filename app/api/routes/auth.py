import logging
from html import escape
from urllib.parse import quote

logger = logging.getLogger(__name__)

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from app.api.dependencies import get_engine, get_api_root_path
from app.auth import browser_login
from app.models.core_models import User
from app.models.response_schemas import ApiKeyRead, LegacyApiKeyRead
from app.utils.permissions import AccessGroupType, Role
from app.utils.auth_utils import decode_token, find_or_add_api_access
from app.utils.sql_utils import add_or_update
from app.utils.user_utils import sql_add_user_to_access_group

# Note: /account/* routes live in account.py

router = APIRouter(tags=["auth"])

SIGN_IN_FAILED = "ORCID sign-in failed; try again"


def _local_path(value) -> str:
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return "/"
    return value


def _login_redirect(login_path: str, return_to: str) -> RedirectResponse:
    response = RedirectResponse(login_path)
    response.set_cookie("after_login_return_to_page", value=return_to, httponly=True, secure=True)
    return response


# ── Canonical routes ─────────────────────────────────────────────────────────

@router.get("/auth/login", response_class=RedirectResponse, status_code=302, responses={302: {"description": "Redirect to the ORCID sign-in page"}, 502: {"description": "ORCID cannot be reached"}})
async def orcid_login(request: Request):
    """Redirect the user to the ORCID login page to begin authentication."""
    try:
        return await browser_login.start_sign_in(request)
    except browser_login.SignInFailed:
        raise HTTPException(status_code=502, detail=SIGN_IN_FAILED) from None


@router.get("/auth/callback", response_class=RedirectResponse, status_code=307, responses={307: {"description": "Redirect back to the page that started sign-in, with a short-lived session cookie"}, 400: {"description": "Sign-in could not be matched to this browser, or ORCID reported an error"}, 502: {"description": "ORCID sign-in failed"}})
async def redirect_exchange(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Finish the ORCID sign-in and set a short-lived session cookie."""
    try:
        user_token = await browser_login.finish_sign_in(request)
    except browser_login.SignInRejected:
        raise HTTPException(status_code=400, detail="Sign-in failed. Start again from the page you came from.") from None
    except browser_login.SignInFailed:
        raise HTTPException(status_code=502, detail=SIGN_IN_FAILED) from None

    response = RedirectResponse(_local_path(request.cookies.get("after_login_return_to_page")))
    response.delete_cookie("after_login_return_to_page")
    response.set_cookie("crucible_user_token", value=user_token, httponly=True, secure=True, max_age=60)
    return response


@router.get("/auth/apikey", response_model=LegacyApiKeyRead, responses={200: {"description": "The API key as JSON, or an HTML page asking for an email when the profile has none", "content": {"text/html": {}}}, 307: {"description": "Redirect to sign-in when there is no ORCID session cookie"}})
def get_apikey(request: Request):
    """Exchange a valid ORCID browser session cookie for a Crucible API key."""
    root = get_api_root_path()
    user_info_jwt = request.cookies.get("crucible_user_token")

    user_info = decode_token(user_info_jwt) if user_info_jwt else None
    if user_info is None:
        return _login_redirect(f"{root}/login", f"{root}/user_apikey")
    orcid = user_info["orcid"]

    with Session(get_engine()) as session:
        # add the user
        key_map = {'orcid': 'unique_id', 'given_name': 'first_name', 'family_name': 'last_name'}
        user_updates = {key_map[k]: user_info[k] for k in key_map if user_info.get(k) is not None}

        existing_user = session.exec(select(User).where(User.unique_id == orcid)).first()
        if existing_user:
            user_updates = {k: v for k, v in user_updates.items() if not getattr(existing_user, k, None)}
        else:
            user_updates.setdefault('first_name', '')
            user_updates.setdefault('last_name', '')

        user = add_or_update(
            session,
            User,
            User.unique_id == orcid,
            **user_updates
        )

        # add the user to their orcid access group
        sql_add_user_to_access_group(
            orcid,
            orcid,
            session,
            role=Role.OWNER,
            group_type=AccessGroupType.IDENTITY,
        )
        session.commit()

        user_email = user.email
        user_first_name = user.first_name
        user_last_name = user.last_name

    crucible_token = find_or_add_api_access(orcid, get_engine(), regenerate=False)

    if not user_email:
        return HTMLResponse(content=_email_prompt_html(root, orcid, crucible_token, user_first_name, user_last_name))

    return {'crucible_apikey': crucible_token}


def _email_prompt_html(root: str, orcid: str, crucible_token: str, first_name: str = '', last_name: str = '') -> str:
    update_url = escape(f"{quote(root, safe='/')}/users/{quote(orcid, safe='')}", quote=True)
    first_name = escape(first_name, quote=True)
    last_name = escape(last_name, quote=True)
    orcid = escape(orcid, quote=True)
    crucible_token = escape(crucible_token, quote=True)
    return f"""<!DOCTYPE html>
<html data-bs-theme="light" lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, shrink-to-fit=no">
<title>Complete Your Profile - Crucible Data Platform</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
<link rel="stylesheet" href="https://fonts.googleapis.com/css?family=Montserrat:400,600,700&display=swap">
<style>
  body {{ font-family: Montserrat, sans-serif; }}
  .navbar-brand {{ font-weight: 700; }}
  #apikey-box {{ font-family: ui-monospace, monospace; font-size: 0.875rem; word-break: break-all; }}
</style>
</head>
<body>
<nav class="navbar navbar-expand-lg fixed-top bg-body clean-navbar navbar-light border-bottom">
  <div class="container">
    <a class="navbar-brand" href="#">Crucible Data Platform</a>
  </div>
</nav>
<main class="page" style="padding-top: 80px;">
  <section class="clean-block clean-form dark py-5">
    <div class="container">
      <div class="block-heading mb-4">
        <h2 class="text-info">Complete Your Profile</h2>
        <p class="text-muted">Add your email to your Crucible profile:</p>
      </div>
      <div id="form-view" style="max-width: 520px;" data-update-url="{update_url}" data-api-key="{crucible_token}">
        <div class="mb-3">
          <label class="form-label" for="first_name">First name</label>
          <input class="form-control" type="text" id="first_name" value="{first_name}" readonly disabled>
        </div>
        <div class="mb-3">
          <label class="form-label" for="last_name">Last name</label>
          <input class="form-control" type="text" id="last_name" value="{last_name}" readonly disabled>
        </div>
        <div class="mb-3">
          <label class="form-label" for="orcid">ORCID</label>
          <input class="form-control" type="text" id="orcid" value="{orcid}" readonly disabled>
        </div>
        <div class="mb-3">
          <label class="form-label" for="email">Email address</label>
          <input class="form-control" type="email" id="email" placeholder="you@example.com" required autofocus>
        </div>
        <div id="error" class="text-danger mb-2" style="display:none;"></div>
        <div class="mb-3">
          <button class="btn btn-primary" id="submit-btn" onclick="submitEmail()">Save and get API key</button>
        </div>
      </div>
      <div id="result-view" style="display:none; max-width: 520px;">
        <h4>Your Crucible API Key</h4>
        <p class="text-muted">Copy this key and store it somewhere safe.</p>
        <div id="apikey-box" class="form-control bg-light"></div>
      </div>
    </div>
  </section>
</main>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
async function submitEmail() {{
  var form = document.getElementById('form-view');
  var email = document.getElementById('email').value.trim();
  var btn = document.getElementById('submit-btn');
  var errorEl = document.getElementById('error');
  errorEl.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {{
    var response = await fetch(form.dataset.updateUrl, {{
      method: 'PATCH',
      headers: {{
        'Authorization': 'Bearer ' + form.dataset.apiKey,
        'Content-Type': 'application/json'
      }},
      body: JSON.stringify({{email: email}})
    }});
    if (response.ok) {{
      document.getElementById('form-view').style.display = 'none';
      document.getElementById('apikey-box').textContent = form.dataset.apiKey;
      document.getElementById('result-view').style.display = 'block';
    }} else {{
      var err = await response.json();
      errorEl.textContent = err.detail || 'Something went wrong. Please try again.';
      errorEl.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Save and get API key';
    }}
  }} catch (e) {{
    errorEl.textContent = 'Network error. Please try again.';
    errorEl.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Save and get API key';
  }}
}}
document.getElementById('email').addEventListener('keydown', function(e) {{
  if (e.key === 'Enter') submitEmail();
}});
</script>
</body>
</html>"""


@router.post("/auth/apikey/regenerate", response_model=ApiKeyRead, responses={307: {"description": "Redirect to sign-in when there is no ORCID session cookie"}})
def regenerate_apikey(request: Request):
    """Regenerate the caller's API key via browser ORCID session. Requires cookie auth."""
    root = get_api_root_path()
    user_info_jwt = request.cookies.get("crucible_user_token")

    user_info = decode_token(user_info_jwt) if user_info_jwt else None
    if user_info is None:
        return _login_redirect(f"{root}/auth/login", f"{root}/auth/apikey")
    orcid = user_info["orcid"]
    crucible_token = find_or_add_api_access(orcid, get_engine(), regenerate=True)
    return {"api_key": crucible_token}


# ── Deprecated aliases (kept for backward compatibility) ─────────────────────

@router.get("/login", deprecated=True, response_class=RedirectResponse, status_code=302, responses={302: {"description": "Redirect to the ORCID sign-in page"}, 502: {"description": "ORCID cannot be reached"}})
async def orcid_login_deprecated(request: Request):
    """Deprecated: use GET /auth/login."""
    return await orcid_login(request)


@router.get("/auth", deprecated=True, response_class=RedirectResponse, status_code=307, responses={307: {"description": "Redirect back to the page that started sign-in, with a short-lived session cookie"}, 400: {"description": "Sign-in could not be matched to this browser, or ORCID reported an error"}, 502: {"description": "ORCID sign-in failed"}})
async def redirect_exchange_deprecated(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    """Deprecated: use GET /auth/callback."""
    return await redirect_exchange(request, code, state, error)


@router.get("/user_apikey", deprecated=True, response_model=LegacyApiKeyRead, responses={200: {"description": "The API key as JSON, or an HTML page asking for an email when the profile has none", "content": {"text/html": {}}}, 307: {"description": "Redirect to sign-in when there is no ORCID session cookie"}})
def get_apikey_deprecated(request: Request):
    """Deprecated: use GET /auth/apikey."""
    return get_apikey(request)
