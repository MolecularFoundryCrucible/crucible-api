import datetime
from typing import Annotated

import requests
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
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
        user = user_from_orcid.json()
        print(f'{user=}')
        exp_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=1)
        user_info_payload = {"orcid": user["sub"], "exp": exp_time}
        user_info_jwt = jwt.encode(user_info_payload, SECRET_KEY, algorithm=ALGORITHM)
    except Exception as err:
        print(err)
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

    api_path = get_api_path()
    user_info_jwt = request.cookies.get("crucible_user_token")

    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie("after_login_return_to_page", value=f"/{api_path}/user_apikey", httponly=True, secure=True)
        return response

    user_info = decode_token(user_info_jwt)
    print(f'{user_info=}')
    orcid = user_info["orcid"]
    crucible_token = find_or_add_api_access(orcid, engine, regenerate=False)

    with Session(engine) as session:
        # confirm access group exists:
        add_or_update(
            session,
            AccessGroup,
            AccessGroup.group_name == orcid,
            group_name=orcid
        )
        # add the user
        # add_or_update(
        #     session,
        #     User,
        #     User.orcid ==orcid,
        #     first_name = user_info[''],
        #     last_name = user_info[''],
        #     email = user_info['email'],
        #   
        # add the user to their orcid access group  
        sql_add_user_to_access_group(orcid, orcid, session)

    return {'crucible_apikey': crucible_token}


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
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        is_user = session.exec(select(User).where(User.orcid == access_group_name)).one_or_none()

    user_info = is_user.model_dump() if is_user else {}

    return AccountInfo(
        access_group_name=access_group_name,
        access_group_ids=ag_ids,
        user_info=user_info
    )
