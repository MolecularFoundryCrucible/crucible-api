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
from app.models.response_schemas import AccountInfo
from app.utils.auth_utils import authorize_user
from app.utils.sql_utils import add_or_update
from app.utils.user_utils import sql_add_user_to_access_group

router = APIRouter(tags=["auth"])
engine = get_engine()


@router.get("/login")
def orcid_login() -> RedirectResponse:
    """
    Redirect the user to the ORCID login page for authentication.

    Returns:
        RedirectResponse to the ORCID login page
    """
    redirect_uri = get_redirect_uri()
    orcid_client_id = get_orcid_client_id()
    orcid_auth_url = f"https://orcid.org/oauth/authorize?response_type=code&redirect_uri={redirect_uri}&client_id={orcid_client_id}&scope=openid&nonce=whatever"
    response = RedirectResponse(orcid_auth_url)
    return response


@router.get("/auth")
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
    from app.utils.auth_utils import ALGORITHM, SECRET_KEY, code_for_token_exchange

    api_url = get_api_url()
    orcid_client_id = get_orcid_client_id()
    orcid_client_secret = get_orcid_client_secret()

    token = code_for_token_exchange(code, orcid_client_id, orcid_client_secret, f"{api_url}/auth")
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


@router.get("/user_apikey")
def get_apikey(request: Request):
    """
    Get the user's API key based on their authentication token.

    Args:
        request: Request object containing the HTTP request

    Returns:
        API key if the user is authenticated, redirects to login page otherwise
    """
    from app.utils.auth_utils import decode_token, find_or_add_api_access

    # if the user is already logged in; get the api key from cookies
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
        sql_add_user_to_access_group(orcid, orcid, session)

        user_email = user.email

    crucible_token = find_or_add_api_access(orcid, engine, regenerate=False)

    if not user_email:
        return HTMLResponse(content=_email_prompt_html(api_path, orcid, crucible_token))

    return {'crucible_apikey': crucible_token}


def _email_prompt_html(api_path: str, orcid: str, crucible_token: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Crucible - Account Setup</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f4f5f7; min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 1rem; }}
  .card {{ background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 2rem; max-width: 460px; width: 100%; }}
  .card-title {{ font-size: 1.15rem; font-weight: 600; color: #24292f; margin-bottom: 0.5rem; }}
  .card-body {{ color: #57606a; font-size: 0.9rem; line-height: 1.6; margin-bottom: 1.5rem; }}
  label {{ display: block; font-size: 0.875rem; font-weight: 600; color: #24292f; margin-bottom: 0.35rem; }}
  input[type="email"] {{ width: 100%; padding: 0.5rem 0.75rem; border: 1px solid #d0d7de; border-radius: 6px; font-size: 0.95rem; color: #24292f; outline: none; transition: border-color 0.15s, box-shadow 0.15s; }}
  input[type="email"]:focus {{ border-color: #0969da; box-shadow: 0 0 0 3px rgba(9,105,218,0.12); }}
  .actions {{ display: flex; gap: 0.75rem; margin-top: 1.25rem; }}
  button {{ padding: 0.5rem 1.25rem; background: #0969da; color: #fff; border: none; border-radius: 6px; font-size: 0.9rem; font-weight: 500; cursor: pointer; transition: background 0.15s; }}
  button:hover {{ background: #0860ca; }}
  button:disabled {{ background: #6cb6ff; cursor: not-allowed; }}
  #error {{ color: #cf222e; font-size: 0.85rem; margin-top: 0.75rem; display: none; }}
  .result-title {{ font-size: 1.1rem; font-weight: 600; color: #24292f; margin-bottom: 0.4rem; }}
  .result-body {{ color: #57606a; font-size: 0.9rem; margin-bottom: 1rem; }}
  #apikey-box {{ background: #f6f8fa; border: 1px solid #d0d7de; padding: 0.75rem; border-radius: 6px; word-break: break-all; font-family: ui-monospace, monospace; font-size: 0.85rem; color: #24292f; }}
</style>
</head>
<body>
<div class="card">
  <div id="form-view">
    <div class="card-title">Crucible — complete your profile</div>
    <p class="card-body">Add your email to your Crucible profile:</p>
    <label for="email">Email address</label>
    <input type="email" id="email" placeholder="you@example.com" required autofocus>
    <div class="actions">
      <button id="submit-btn" onclick="submitEmail()">Save and get API key</button>
    </div>
    <div id="error"></div>
  </div>
  <div id="result-view" style="display:none">
    <div class="result-title">Your Crucible API key</div>
    <p class="result-body">Copy this key and store it somewhere safe — it grants access to your Crucible account.</p>
    <div id="apikey-box"></div>
  </div>
</div>
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
      btn.textContent = 'Continue to API key';
    }}
  }} catch (e) {{
    errorEl.textContent = 'Network error. Please try again.';
    errorEl.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Continue to API key';
  }}
}}
document.getElementById('email').addEventListener('keydown', function(e) {{
  if (e.key === 'Enter') submitEmail();
}});
</script>
</body>
</html>"""


@router.get("/regenerate_apikey")
def regenerate_apikey(request: Request):
    """
    Regenerate the user's API key based on their authentication token.

    Args:
        request: Request object containing the HTTP request

    Returns:
        New API key if the user is authenticated, redirects to login page otherwise
    """
    from app.utils.auth_utils import decode_token, find_or_add_api_access

    api_path = get_api_path()
    user_info_jwt = request.cookies.get("crucible_user_token")

    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie("after_login_return_to_page", value=f"/{api_path}/regenerate_apikey", httponly=True, secure=True)
        return response

    user_info = decode_token(user_info_jwt)
    orcid = user_info["orcid"]
    crucible_token = find_or_add_api_access(orcid, engine, regenerate=True)

    return f"Your New Crucible APIkey is: {crucible_token}"


@router.get("/account", response_model=AccountInfo)
def get_account_info(auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Use API key to get basic info about your access in Crucible"""
    user_unique_id, ag_list = auth_res
    ag_ids = [ag.id for ag in ag_list]
    with Session(engine) as session:
        is_user = session.exec(select(User).where(User.unique_id == user_unique_id)).one_or_none()
        user_info = is_user.model_dump(exclude={"projects", "accessgroups"}) if is_user else {}

    return AccountInfo(
        user_unique_id=user_unique_id,
        access_group_ids=ag_ids,
        user_info=user_info
    )
