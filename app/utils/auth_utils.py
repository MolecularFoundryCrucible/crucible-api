from typing import Annotated
import logging
import re
import requests
import secrets
import datetime
from jose import jwt, JWTError

logger = logging.getLogger(__name__)

from fastapi import Depends,  HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.general_utils import  get_secret
from app.api.dependencies import get_engine, get_api_path, get_api_url
from app.models.core_models import ApiToken
from sqlmodel import Session, select
from sqlalchemy.sql import and_
from sqlalchemy import join
from datetime import date, timedelta

# ====== Constants
SECRET_KEY = get_secret("CRUCIBLE_SECRET_KEY", "crucible_secret_key/versions/1")
ALGORITHM = "HS256"
security = HTTPBearer()

engine = get_engine()
api_path = get_api_path()
api_url = get_api_url()

# ====== Functions
def find_or_add_api_access(orcid: str, engine, regenerate = False):

    # query the database for orcid
    one_year_ago = (date.today()-timedelta(365)).isoformat()
    with Session(engine) as session:
        api_acct = session.exec(select(ApiToken).where(and_(ApiToken.user_unique_id == orcid,
                                                            ApiToken.token_created > one_year_ago))).one_or_none()

        if api_acct is not None and regenerate == False:
            return(api_acct.token)

        elif api_acct is not None and regenerate == True:
            delete_apikey(orcid, session)
            new_token = generate_apikey(orcid, session)
            return(new_token)

        else:
            new_token = generate_apikey(orcid, session)
            return(new_token)


def generate_apikey(orcid: str, session):
    # create token
    new_token = secrets.token_hex(32)
    # add to database
    new_api_acct = ApiToken(user_unique_id = orcid,
                            access_group_id = orcid,
                            token = new_token,
                            token_created = date.today().isoformat())
    session.add(new_api_acct)
    session.commit()
    return(new_token)

    
def delete_apikey(orcid, session):
    session.query(ApiToken).filter(ApiToken.user_unique_id == orcid).delete(synchronize_session=False)
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
        login_path = ''.join(api_path, '/login')
        return RedirectResponse(login_path)


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
    apikey = credentials.credentials
    one_year_ago = (date.today()-timedelta(365)).isoformat()
    with Session(engine) as session:
        cmd = select(ApiToken).where(and_(ApiToken.token == apikey,
                                          ApiToken.token_created > one_year_ago))
        api_acct = session.exec(cmd).one_or_none()

        if api_acct is None:
            cmd = select(ApiToken).where(ApiToken.token == apikey)
            expired_api_acct = session.exec(cmd).one_or_none()
            if expired_api_acct is None:
                raise HTTPException(status_code=400, detail="No account found for this API key.")
            else:
                logger.warning(f"Expired API account found: {expired_api_acct}")
                created_date = datetime.datetime.strptime(expired_api_acct.token_created, '%Y-%m-%d')
                expiration_date = (created_date + timedelta(365)).isoformat()
                raise HTTPException(status_code=400, detail=f"This API key expired on {expiration_date}")

    return(api_acct)


def authorize_user(credentials:  Annotated[HTTPAuthorizationCredentials, Depends(security)]):
    """
    Authorize a user based on their API credentials.

    Args:
        credentials: HTTPAuthorizationCredentials object containing the API token

    Returns:
        Tuple of (user_unique_id, list of AccessGroup objects the user belongs to)

    Raises:
        HTTPException: If the API key is invalid or expired
    """
    from app.models.core_models import User
    api_acct = find_key(credentials, engine)
    
    if api_acct is None:
        raise HTTPException(status_code=400, detail="Invalid apikey.")
    
    with Session(engine) as session:
        user = session.exec(                                                                                                                                      
            select(User).where(User.unique_id == api_acct.user_unique_id)).first()                                                                                                                                                 
        ag = user.accessgroups  # relationship handles the join for you                
        return api_acct.user_unique_id, ag


def require_read_access(session, mfid: str, ag_ids: list):
    """Raise 403 if the user has no read access to the given MFID.

    Access is granted if Resource.public is True OR the user has an ACL entry.
    Call after a 404 check so the caller can distinguish not-found from forbidden.
    """
    from sqlalchemy import exists, select
    from app.models.core_models import Resource, ResourceACL

    acl_ok = session.exec(
        select(Resource).where(Resource.mfid == mfid).where(
            (Resource.public == True) |
            exists().where(
                ResourceACL.mfid == mfid,
                ResourceACL.accessgroup_id.in_(ag_ids)
            )
        )
    ).first()
    if not acl_ok:
        raise HTTPException(status_code=403, detail="Access denied")


def read_access_filter_list(resource_model, ag_ids: list):
    """
    Returns (auth_filter, deletion_filter) for list queries on a resource model.

    Requires the query to join Resource on Resource.mfid == resource_model.unique_id.
    auth_filter: visible if Resource.public is True OR user has an ACL entry.
    deletion_filter: excludes resources with a pending or approved deletion request.
    """
    from sqlalchemy import exists
    from app.models.core_models import Resource, ResourceACL, DeletionRequest

    acl_subquery = exists().where(
        ResourceACL.mfid == resource_model.unique_id,
        ResourceACL.accessgroup_id.in_(ag_ids)
    )
    auth_filter = (Resource.public == True) | acl_subquery
    deletion_filter = ~exists().where(
        DeletionRequest.resource_id == resource_model.unique_id,
        DeletionRequest.status.in_(["pending", "approved"])
    )
    return auth_filter, deletion_filter


def require_write_access(session, ag_ids: list, mfids: list):
    """Raise 403 if the user has no write ACL on any of the given MFIDs."""
    from app.models.core_models import ResourceACL
    from sqlmodel import select
    acl_ok = session.exec(
        select(ResourceACL).where(
            ResourceACL.mfid.in_(mfids),
            ResourceACL.accessgroup_id.in_(ag_ids),
            ResourceACL.write == True
        )
    ).first()
    if not acl_ok:
        raise HTTPException(status_code=403, detail="Write access required to at least one of the resources")


def require_groups(ag_list, allowed_groups={'admin'}):
    if not any(ag.group_name in allowed_groups for ag in ag_list):
        raise HTTPException(status_code=403, detail="Not authorized")


def is_admin(credentials: Annotated[HTTPAuthorizationCredentials, Depends(security)]):
    """
    Check if a user is an admin based on their API credentials.
    
    Args:
        credentials: HTTPAuthorizationCredentials object containing the API token
        
    Returns:
        "admin" if the user is an admin, raises HTTPException otherwise
        
    This function validates the provided API credentials and checks if the user belongs to the 'admin' access group.
    """
    _, aglist = authorize_user(credentials)
    
    if 'admin' in [ag.group_name for ag in aglist]:
        return('admin')

