from typing import Annotated
import re
import os
import requests
import secrets
import datetime
from jose import jwt, JWTError

from fastapi import Depends,  HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.general_utils import  get_secret, is_orcid
from app.utils.sql_utils import setup_engine
from sqlmodel import Session, select
from sqlalchemy.sql import and_
from sqlalchemy import select, join
from datetime import date, timedelta


# ====== Constants
SECRET_KEY = get_secret("CRUCIBLE_SECRET_KEY", "crucible_secret_key/versions/1")
ALGORITHM = "HS256"
security = HTTPBearer()

engine = setup_engine(env_file = '.env')

# ====== Functions
def find_or_add_api_access(orcid: str, engine, instrument_name = None, regenerate = False):
    from app.models.misc import ApiToken

    # query the database for orcid
    one_year_ago = (date.today()-timedelta(365)).isoformat()
    with Session(engine) as session:
        api_acct = session.exec(select(ApiToken).where(and_(ApiToken.access_group_id == orcid,
                                                            ApiToken.token_created > one_year_ago))).one_or_none()

    if api_acct is not None and regenerate == False:
        print(f"found an account")
        return(api_acct[0].token)
        
    elif api_acct is not None and regenerate == True:
        delete_apikey(orcid, session)
        new_token = generate_apikey(orcid, session)
        return(new_token)
        
    else: 
        new_token = generate_apikey(orcid, session)
        return(new_token)

# def find_or_add_api_access_for_service(service: str, engine, regenerate = False):
#     from app.models.misc import ApiToken

#     # query the database for orcid
#     one_year_ago = (date.today()-timedelta(365)).isoformat()
#     with Session(engine) as session:
#         api_acct = session.exec(select(ApiToken).where(and_(ApiToken.access_group_id == service,
#                                                             ApiToken.token_created > one_year_ago))).one_or_none()

#     if api_acct is not None and regenerate == False:
#         print(f"found an account")
#         return(api_acct[0].token)
        
#     elif api_acct is not None and regenerate == True:
#         delete_apikey(service, session)
#         new_token = generate_apikey(orcid, session)
#         return(new_token)
        
#     else: 
#         new_token = generate_apikey(orcid, session)
#         return(new_token)


def generate_apikey(orcid: str, session):
    from app.models.misc import ApiToken
    print(f'making a new acct')
    # create token
    new_token = secrets.token_hex(32)
    # add to database
    new_api_acct = ApiToken(access_group_id = orcid,
                            token = new_token,
                            token_created = date.today().isoformat())
    session.add(new_api_acct)
    session.commit()
    return(new_token)

    
def delete_apikey(orcid, session):
    from app.models.misc import ApiToken
    session.query(ApiToken).filter(ApiToken.access_group_id == orcid).delete(synchronize_session=False)
    session.commit()


def code_for_token_exchange(code: str, client_id, client_secret, redirect_uri):
    token_req = requests.post(url = "https://orcid.org/oauth/token", 
                              data = {  "client_id": client_id,
                                        "client_secret": client_secret, 
                                        "grant_type":"authorization_code", 
                                        "code":code, 
                                        "redirect_uri":redirect_uri
                                     }, 
                              headers = {'Accept': 'application/json'})   
    
    token = token_req.json()['access_token']
    return(token)


def decode_token(token: str, secret_key = SECRET_KEY, alg = ALGORITHM):
    try:
        payload = jwt.decode(token, secret_key, algorithms=[alg])
        return payload
    except JWTError:
        return RedirectResponse("/testapi/login")


def find_key(credentials, engine):
    """
    Find an API token record in the database matching the provided credentials.
    
    Args:
        credentials: HTTPAuthorizationCredentials object containing the API token
        
    Returns:
        ApiToken record if found and not expired (created within last year), None otherwise
        
    The function checks if the provided API token exists in the database and was created
    within the last year. This is used for authenticating API requests.
    """
    from app.models.misc import ApiToken
    apikey = credentials.credentials
    one_year_ago = (date.today()-timedelta(365)).isoformat()
    print(f"{one_year_ago=}")
    with Session(engine) as session:
        api_acct = session.exec(select(ApiToken).where(and_(ApiToken.token == apikey, ApiToken.token_created > one_year_ago))).one_or_none()
    if api_acct is None:
        expired_api_acct = session.exec(select(ApiToken).where(and_(ApiToken.token == apikey))).one_or_none()
        if expired_api_acct is None:
            raise HTTPException(status_code=400, detail="No account found for this API key.")
        else:
            created_date = datetime.datetime.strptime(expired_api_acct.token_created, '%Y-%m-%d')
            expiration_date = created_date + timedelta(365).isoformat()
            raise HTTPException(status_code=400, detail=f"This API key expired on {expiration_date}")
    return(api_acct)


def authorize_user(credentials:  Annotated[HTTPAuthorizationCredentials, Depends(security)]):
    """
    Authorize a user based on their API credentials.
    
    Args:
        credentials: HTTPAuthorizationCredentials object containing the API token
        
    Returns:
        List of access group IDs the user belongs to
        
    Raises:
        HTTPException: If the API key is invalid or expired
        
    This function validates the provided API credentials and returns the access groups
    associated with the user. For ORCID-based users, it looks up their access groups
    through the User and UserAccessGroupLink tables. For direct access group users (provided API key corrresponds to a group name not an ORCID; 
    note: ORCIDs are used as Access Group Names, but a user with an ORCID can be a member of multiple access groups),
    it looks up the access group ID directly.
    """
    from app.models.core_models import UserAccessGroupLink, User, AccessGroup
    api_acct = find_key(credentials, engine)
    
    if api_acct is None:
        raise HTTPException(status_code=400, detail="Incorrect apikey.  Please try again or visit https://crucible.lbl.gov/api/regenerate_apikey to create a new key.")
    else:
        access_group_name = api_acct[0].access_group_id

    # If the APIKEY is associated with an ORCID; make sure that ORCID is a user in our database
    if is_orcid(access_group_name):
        with Session(engine) as session:
            ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == access_group_name)).all()
        ag_ids = [x[0].accessgroup_id for x in ag]
        return(access_group_name, ag_ids)
        
    else:
        with Session(engine) as session:
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).all()
        ag_ids = [x[0].id for x in ag]
        return(access_group_name, ag_ids)  


def is_admin(credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)]):
    """
    Check if a user is an admin based on their API credentials.
    
    Args:
        credentials: HTTPAuthorizationCredentials object containing the API token
        
    Returns:
        Access group ID if the user is an admin, raises HTTPException otherwise
        
    This function validates the provided API credentials and checks if the user belongs to the 'admin' access group.
    """
    api_acct = find_key(credentials, engine)
    if api_acct[0].access_group_id.startswith('admin'):
        return(api_acct[0].access_group_id)
    else:
        raise HTTPException(status_code=403, detail="Not authorized for this endpoint")

