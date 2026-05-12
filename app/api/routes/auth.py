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
        update_fields = {'unique_id': 'orcid',
                         'first_name': 'given_name', 
                         'last_name': 'family_name'}
        
        user_updates = {k:user_info.get(v) for k,v in update_fields.items() if user_info.get(v) is not None}
        print(f'{user_updates=}')
        
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
<title>Crucible - Email Required</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #1a1a2e; min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .card {{ background: #fff; border-radius: 12px; padding: 2rem; max-width: 420px; width: 90%; box-shadow: 0 20px 60px rgba(0,0,0,0.4); }}
  h1 {{ font-size: 1.4rem; color: #1a1a2e; margin-bottom: 0.5rem; }}
  p {{ color: #555; font-size: 0.95rem; line-height: 1.5; margin-bottom: 1.5rem; }}
  label {{ display: block; font-size: 0.875rem; font-weight: 600; color: #333; margin-bottom: 0.4rem; }}
  input[type="email"] {{ width: 100%; padding: 0.65rem 0.9rem; border: 1px solid #ccc; border-radius: 6px; font-size: 1rem; outline: none; transition: border-color 0.2s; }}
  input[type="email"]:focus {{ border-color: #4a6cf7; box-shadow: 0 0 0 3px rgba(74,108,247,0.15); }}
  button {{ width: 100%; margin-top: 1rem; padding: 0.75rem; background: #4a6cf7; color: #fff; border: none; border-radius: 6px; font-size: 1rem; font-weight: 600; cursor: pointer; transition: background 0.2s; }}
  button:hover {{ background: #3a5ce5; }}
  button:disabled {{ background: #9aaaf7; cursor: not-allowed; }}
  #error {{ color: #c0392b; font-size: 0.875rem; margin-top: 0.75rem; display: none; }}
  #apikey-box {{ background: #f5f5f5; padding: 0.75rem; border-radius: 6px; word-break: break-all; font-family: monospace; font-size: 0.9rem; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <div id="form-view">
    <h1>One more step</h1>
    <p>Your ORCID profile does not include an email address. Please provide one so we can contact you about your data.</p>
    <label for="email">Email address</label>
    <input type="email" id="email" placeholder="you@example.com" required autofocus>
    <button id="submit-btn" onclick="submitEmail()">Continue to API key</button>
    <div id="error"></div>
  </div>
  <div id="result-view" style="display:none">
    <h1>Your API Key</h1>
    <p>Save this key — it won't be shown again here.</p>
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
