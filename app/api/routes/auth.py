import datetime
import logging
from typing import Annotated

logger = logging.getLogger(__name__)

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jose import jwt
from sqlmodel import Session, select

from app.api.dependencies import (
    get_engine, get_api_path, get_api_url,
    get_orcid_client_id, get_orcid_client_secret, get_redirect_uri,
)
from app.models.core_models import AccessGroup, User
from app.utils.permissions import Role
from app.utils.auth_utils import find_or_add_api_access
from app.utils.sql_utils import add_or_update
from app.utils.user_utils import sql_add_user_to_access_group

# Note: /account/* routes live in account.py

router = APIRouter(tags=["auth"])
engine = get_engine()


# ── Canonical routes ─────────────────────────────────────────────────────────

@router.get("/auth/login")
def orcid_login() -> RedirectResponse:
    """Redirect the user to the ORCID login page to begin authentication."""
    redirect_uri = get_redirect_uri()
    orcid_client_id = get_orcid_client_id()
    orcid_auth_url = f"https://orcid.org/oauth/authorize?response_type=code&redirect_uri={redirect_uri}&client_id={orcid_client_id}&scope=openid&nonce=whatever"
    return RedirectResponse(orcid_auth_url)


@router.get("/auth/callback")
def redirect_exchange(request: Request, code: str, state: str = None):
    """
    Exchange the ORCID authorization code for an access token and user information.

    Args:
        request: Request object containing the HTTP request
        code: ORCID authorization code
        state: Optional state parameter (default is None)

    Returns:
        RedirectResponse to the appropriate page after successful authentication
    """
    import urllib.parse
    from app.utils.auth_utils import ALGORITHM, SECRET_KEY, code_for_token_exchange

    orcid_client_id = get_orcid_client_id()
    orcid_client_secret = get_orcid_client_secret()
    redirect_uri = urllib.parse.unquote_plus(get_redirect_uri())

    token = code_for_token_exchange(code, orcid_client_id, orcid_client_secret, redirect_uri)
    user_from_orcid = requests.get(
        url="https://orcid.org/oauth/userinfo",
        headers={"Authorization": f"Bearer {token}"}
    )
    try:
        user_info_payload = user_from_orcid.json()
        exp_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
        user_info_payload['orcid'] = user_info_payload['sub']
        user_info_payload['exp'] = exp_time
        user_info_jwt = jwt.encode(user_info_payload, SECRET_KEY, algorithm=ALGORITHM)
    except Exception as err:
        logger.error(f"Error encoding ORCID user info: {err}")
        user_info_jwt = None

    next_page = request.cookies.get("after_login_return_to_page")
    response = RedirectResponse("/" if next_page == "None" else next_page)
    response.set_cookie("after_login_return_to_page", value=None, httponly=True, secure=True)
    response.set_cookie(
        "crucible_user_token",
        value=user_info_jwt,
        httponly=True,
        secure=True,
        max_age=60
    )
    return response


@router.get("/auth/apikey")
def get_apikey(request: Request):
    """Exchange a valid ORCID browser session cookie for a Crucible API key."""
    from app.utils.auth_utils import decode_token, find_or_add_api_access

    api_path = get_api_path()
    user_info_jwt = request.cookies.get("crucible_user_token")

    # otherwise direct to login
    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie("after_login_return_to_page", value=f"/{api_path}/user_apikey", httponly=True, secure=True)
        return response

    # get the user info
    user_info = decode_token(user_info_jwt)
    logger.debug(f"Orcid user profile: {user_info}")
    orcid = user_info["orcid"]

    with Session(engine) as session:
        # confirm access group for the user's orcid exists
        add_or_update(
            session,
            AccessGroup,
            AccessGroup.group_name == orcid,
            group_name=orcid
        )
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
        sql_add_user_to_access_group(orcid, orcid, session, role=Role.OWNER)
        session.commit()

        user_email = user.email
        user_first_name = user.first_name
        user_last_name = user.last_name

    crucible_token = find_or_add_api_access(orcid, engine, regenerate=False)

    if not user_email:
        return HTMLResponse(content=_email_prompt_html(api_path, orcid, crucible_token, user_first_name, user_last_name))

    return {'crucible_apikey': crucible_token}


def _email_prompt_html(api_path: str, orcid: str, crucible_token: str, first_name: str = '', last_name: str = '') -> str:
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
      <div id="form-view" style="max-width: 520px;">
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
  var email = document.getElementById('email').value.trim();
  var btn = document.getElementById('submit-btn');
  var errorEl = document.getElementById('error');
  errorEl.style.display = 'none';
  btn.disabled = true;
  btn.textContent = 'Saving...';
  try {{
    var response = await fetch('/{api_path}/users/{orcid}', {{
      method: 'PATCH',
      headers: {{
        'Authorization': 'Bearer {crucible_token}',
        'Content-Type': 'application/json'
      }},
      body: JSON.stringify({{email: email}})
    }});
    if (response.ok) {{
      document.getElementById('form-view').style.display = 'none';
      document.getElementById('apikey-box').textContent = '{crucible_token}';
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


@router.post("/auth/apikey/regenerate")
def regenerate_apikey(request: Request):
    """Regenerate the caller's API key via browser ORCID session. Requires cookie auth."""
    from app.utils.auth_utils import decode_token

    api_path = get_api_path()
    user_info_jwt = request.cookies.get("crucible_user_token")

    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/auth/login")
        response.set_cookie("after_login_return_to_page", value=f"/{api_path}/auth/apikey", httponly=True, secure=True)
        return response

    user_info = decode_token(user_info_jwt)
    orcid = user_info["orcid"]
    crucible_token = find_or_add_api_access(orcid, engine, regenerate=True)
    return {"api_key": crucible_token}


# ── Deprecated aliases (kept for backward compatibility) ─────────────────────

@router.get("/login", deprecated=True, include_in_schema=True)
def orcid_login_deprecated() -> RedirectResponse:
    """Deprecated: use GET /auth/login."""
    return orcid_login()


@router.get("/auth", deprecated=True, include_in_schema=True)
def redirect_exchange_deprecated(request: Request, code: str, state: str = None):
    """Deprecated: use GET /auth/callback."""
    return redirect_exchange(request, code, state)


@router.get("/user_apikey", deprecated=True, include_in_schema=True)
def get_apikey_deprecated(request: Request):
    """Deprecated: use GET /auth/apikey."""
    return get_apikey(request)


@router.get("/regenerate_apikey", deprecated=True, include_in_schema=True)
def regenerate_apikey_deprecated(request: Request):
    """Deprecated: use POST /auth/apikey/regenerate."""
    return regenerate_apikey(request)
