import datetime
import json
import os
import urllib.parse
from pathlib import Path
from typing import Annotated, Dict, List, Optional, Tuple, Union

import pytz
import requests
import yaml
from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBearer
from jose import jwt
from sqlalchemy import func, join, text
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.sql import and_
from sqlmodel import Session, select
from starlette.background import BackgroundTasks

from app.general_utils import get_secret, run_rclone_command, remove_file, get_tz_isoformat, is_orcid
from app.utils.sql_utils import setup_engine, add_or_update, only_update, table_obj_to_dict
from app.utils.rmq_utils import send_pika_message
from app.utils.constants import SAMPLE_TYPE_MAPPING


from app.models.core_models import (
    AccessGroup,
    AssociatedFile,
    Dataset,
    DatasetACL,
    DatasetCollection,
    DatasetCollectionLink,
    DatasetKeywordLink,
    DatasetSampleLink,
    Keyword,
    Project,
    Sample,
    SampleLink,
    ScientificMetadata,
    Thumbnail,
    User,
    UserAccessGroupLink,
    UserProjectLink,
)

from app.models.instrument import Instrument
from app.models.misc import IdType, DataDriveLocation, GoogleDrives
from app.models.tasks import GoogleDriveTransferRequest, SciCatUpdateRequest, IngestionRequest
from app.models.response_schemas import SampleWithDatasetsResponse, DatasetSampleLinkResponse, AccountInfo
from app.models.sample_metadata_models import SpinbotBatchMetadata


# ====== Database config
connect_args = {"check_same_thread": False}

home_dir = os.getenv("HOME")
db_config_tmp = os.getenv("DB_CONF_FILE")
db_config_file = f"{home_dir}/{db_config_tmp}"

engine = setup_engine(db_config_file)

api_config_file = os.getenv("API_CONF_FILE")
with open(f"{home_dir}/{api_config_file}", 'r') as f:
    apiconf = yaml.safe_load(f)

hostname = apiconf['hostname']
api_path = apiconf['api_path']
api_url = f"https://{hostname}/{api_path}"

# ======================= APP
app = FastAPI(root_path=f"/{api_path}")
security = HTTPBearer()
orcid_client_id = apiconf['orcid_client_id']
orcid_client_secret = get_secret("CLIENT_SECRET", "orcid_client_secret/versions/2")
redirect_uri = urllib.parse.quote_plus(f"{api_url}/auth")




# ======================= AUTH AND ACCOUNT SET UP ENDPOINTS
from app.utils.auth_utils import authorize_user, is_admin

@app.get("/login")
def orcid_login() -> RedirectResponse:
    """Initiate ORCID authentication by redirecting to ORCID login page.

    Generates a secure CSRF state token and redirects the user to the ORCID OAuth
    authorization page for authentication. The CSRF token is stored in a secure
    cookie to prevent cross-site request forgery attacks during the OAuth flow.

    Returns:
        RedirectResponse: Redirect to ORCID OAuth authorization URL

    Security Features:
        - CSRF state parameter generation and validation
        - Secure, HttpOnly cookie storage for state token
        - 30-minute expiration for authentication flow
        - SameSite=strict policy for additional CSRF protection

    OAuth Flow:
        1. Generate and store CSRF state token
        2. Redirect to ORCID with authorization request
        3. User authenticates with ORCID
        4. ORCID redirects back to /auth endpoint
        5. State token validated and user session established

    URL Parameters:
        - response_type=code: Authorization code flow
        - client_id: Registered ORCID application identifier
        - scope=openid: Request basic profile access
        - state: CSRF protection token
        - redirect_uri: Callback URL for authentication completion

    Note:
        This begins the OAuth 2.0 authorization code flow with ORCID.
        Users must have valid ORCID accounts to authenticate.
    """
    from app.utils.auth_utils import generate_csrf_state

    # Generate CSRF state parameter
    csrf_state = generate_csrf_state()

    orcid_auth_url = (
        f"https://orcid.org/oauth/authorize?response_type=code&"
        f"redirect_uri={redirect_uri}&client_id={orcid_client_id}&"
        f"scope=openid&state={csrf_state}"
    )
    response = RedirectResponse(orcid_auth_url)

    # Store CSRF state in secure cookie for validation
    response.set_cookie(
        "crucible_csrf_state",
        value=csrf_state,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=1800  # 30 minutes
    )
    return response


@app.get("/auth")
def redirect_exchange(request: Request, code: str, state: str = None):
    """Complete ORCID OAuth flow by exchanging authorization code for user session.

    Validates the CSRF state token, exchanges the authorization code for an access
    token, retrieves user information from ORCID, and establishes an authenticated
    user session with a JWT token.

    Args:
        request: FastAPI Request object containing cookies and session data
        code: Authorization code received from ORCID OAuth callback
        state: CSRF state token for validation (optional but required for security)

    Returns:
        RedirectResponse: Redirect to original page or home after authentication

    Raises:
        HTTPException: 400 if CSRF state validation fails

    Security Validation:
        - CSRF state token comparison with stored cookie
        - Authorization code verification with ORCID
        - JWT token generation with expiration
        - Secure cookie cleanup after authentication

    Authentication Flow:
        1. Validate CSRF state parameter against stored cookie
        2. Exchange authorization code for ORCID access token
        3. Retrieve user profile information from ORCID
        4. Generate JWT session token with 30-minute expiration
        5. Set secure session cookie and redirect user
        6. Clean up temporary authentication cookies

    Session Management:
        - JWT tokens expire after 30 minutes for security
        - HttpOnly, Secure, SameSite=strict cookie settings
        - Automatic cleanup of temporary authentication state

    Error Handling:
        - Invalid state tokens result in 400 Bad Request
        - ORCID API failures fall back gracefully
        - Failed authentication redirects to login

    Note:
        This completes the OAuth 2.0 flow and establishes user sessions.
        Subsequent API calls use the JWT token for authentication.
    """
    from app.utils.auth_utils import SECRET_KEY, ALGORITHM, code_for_token_exchange

    # Validate CSRF state
    stored_state = request.cookies.get("crucible_csrf_state")
    if not state or not stored_state or state != stored_state:
        raise HTTPException(status_code=400, detail="Invalid or missing CSRF state parameter")

    token = code_for_token_exchange(
        code, orcid_client_id, orcid_client_secret, f"{api_url}/auth"
    )
    user_from_orcid = requests.get(
        url="https://orcid.org/oauth/userinfo",
        headers={"Authorization": f"Bearer {token}"}
    )
    try:
        user = user_from_orcid.json()
        # Extended JWT expiry to 30 minutes for better UX
        exp_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
        user_info_payload = {"orcid": user["sub"], "exp": exp_time}
        user_info_jwt = jwt.encode(user_info_payload, SECRET_KEY, algorithm=ALGORITHM)

    except Exception as err:
        print(err)
        user_info_jwt = None

    next_page = request.cookies.get("after_login_return_to_page")
    if next_page == 'None':
        response = RedirectResponse("/")
    else:
        response = RedirectResponse(next_page)

    # Clear temporary cookies
    response.set_cookie(
        "after_login_return_to_page",
        value=None,
        httponly=True,
        secure=True,
        samesite="strict"
    )
    response.set_cookie(
        "crucible_csrf_state",
        value=None,
        httponly=True,
        secure=True,
        samesite="strict"
    )

    # Set user token with improved security
    response.set_cookie(
        "crucible_user_token",
        value=user_info_jwt,
        httponly=True,
        secure=True,
        samesite="strict",
        max_age=1800  # 30 minutes
    )
    return response


@app.get("/user_apikey")
def get_apikey(request: Request):
    """Retrieve or generate an API key for the authenticated user.

    Validates the user's session token and returns their personal API key for
    programmatic access to the system. Creates a new API key if none exists,
    and ensures the user has appropriate access group membership.

    Args:
        request: FastAPI Request object containing session cookies

    Returns:
        JSON: API key information and usage instructions
        RedirectResponse: To login page if not authenticated

    Response Format:
        ```json
        {
            "api_key": "string",
            "user_orcid": "0000-0000-0000-0000",
            "message": "API key generated successfully",
            "usage": "Include this key in the Authorization header as 'Bearer <api_key>' for API requests"
        }
        ```

    Authentication Flow:
        1. Validate JWT session token from cookie
        2. Extract user ORCID from verified token
        3. Generate or retrieve existing API key
        4. Ensure user access group exists
        5. Return API key with usage instructions

    Access Control Setup:
        - Creates personal access group named after user's ORCID
        - Associates user with their personal access group
        - Enables dataset and resource access control

    Error Handling:
        - Invalid/expired tokens redirect to login
        - Missing tokens redirect to login with return path
        - Token decode failures handled gracefully

    Use Cases:
        - Programmatic API access for scripts and applications
        - Integration with external tools and workflows
        - Automated data processing and analysis

    Note:
        API keys provide long-term authentication for programmatic access.
        Store keys securely and use HTTPS for all API requests.
    """
    from app.utils.auth_utils import decode_token, find_or_add_api_access
    user_info_jwt = request.cookies.get("crucible_user_token")

    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie(
            "after_login_return_to_page",
            value=f"/{api_path}/user_apikey",
            httponly=True,
            secure=True,
            samesite="strict"
        )
        return response

    try:
        user_info = decode_token(user_info_jwt)
        orcid = user_info['orcid']
        crucible_token = find_or_add_api_access(orcid, engine, regenerate=False)

        with Session(engine) as session:
            # confirm access group exists:
            new_access_group = add_or_update(
                session,
                AccessGroup,
                AccessGroup.group_name == orcid,
                group_name=orcid
            )

        return {
            "api_key": crucible_token,
            "user_orcid": orcid,
            "message": "API key generated successfully",
            "usage": "Include this key in the Authorization header as 'Bearer <api_key>' for API requests"
        }

    except Exception as e:
        # Handle token decode errors or other issues
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie(
            "after_login_return_to_page",
            value=f"/{api_path}/user_apikey",
            httponly=True,
            secure=True,
            samesite="strict"
        )
        return response


@app.get("/regenerate_apikey")
def regenerate_apikey(request: Request):
    """Generate a new API key for the authenticated user, invalidating the old one.

    Creates a fresh API key for the user while invalidating their previous key.
    This is useful for security purposes when a key may have been compromised
    or for regular key rotation practices.

    Args:
        request: FastAPI Request object containing session cookies

    Returns:
        JSON: New API key information with security warning
        RedirectResponse: To login page if not authenticated

    Response Format:
        ```json
        {
            "api_key": "string",
            "user_orcid": "0000-0000-0000-0000",
            "message": "New API key generated successfully",
            "warning": "Your previous API key has been invalidated",
            "usage": "Include this key in the Authorization header as 'Bearer <api_key>' for API requests"
        }
        ```

    Security Features:
        - Immediate invalidation of previous API key
        - New key generation with secure random values
        - Warning about key invalidation
        - Session token validation required

    Regeneration Process:
        1. Validate JWT session token from cookie
        2. Extract user ORCID from verified token
        3. Invalidate existing API key
        4. Generate new secure API key
        5. Return new key with security warning

    Use Cases:
        - Security incident response (compromised keys)
        - Regular security key rotation
        - Troubleshooting authentication issues
        - Revoking access for lost/stolen credentials

    Important:
        - Previous API key becomes immediately invalid
        - All applications using old key must be updated
        - New key provides same access permissions as before

    Note:
        Key regeneration is irreversible. Ensure all applications
        are updated with the new key to maintain access.
    """
    from app.utils.auth_utils import decode_token, find_or_add_api_access
    user_info_jwt = request.cookies.get("crucible_user_token")

    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie(
            "after_login_return_to_page",
            value=f"/{api_path}/regenerate_apikey",
            httponly=True,
            secure=True,
            samesite="strict"
        )
        return response

    try:
        user_info = decode_token(user_info_jwt)
        orcid = user_info['orcid']
        crucible_token = find_or_add_api_access(orcid, engine, regenerate=True)

        return {
            "api_key": crucible_token,
            "user_orcid": orcid,
            "message": "New API key generated successfully",
            "warning": "Your previous API key has been invalidated",
            "usage": "Include this key in the Authorization header as 'Bearer <api_key>' for API requests"
        }

    except Exception as e:
        # Handle token decode errors or other issues
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie(
            "after_login_return_to_page",
            value=f"/{api_path}/regenerate_apikey",
            httponly=True,
            secure=True,
            samesite="strict"
        )
        return response


########################

@app.get("/account", response_model=AccountInfo)
def get_account_info(auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Use API key to get basic info about your access in Crucible."""
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        is_user = session.exec(select(User).where(User.orcid == access_group_name)).one_or_none()

    if is_user is not None:
        user_info = is_user.model_dump()
    else:
        user_info = {}

    account_info = AccountInfo(
        access_group_name=access_group_name,
        access_group_ids=ag_ids,
        user_info=user_info
    )
    return account_info


# TODO: person creating dataset must exist in create data set access group
# TODO: public datasets rate limited but dont require orcid
# TODO: use agids to authorize for all endpoints
@app.post("/datasets", response_model=Dataset)
async def create_dataset(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dataset: Dataset
):
    """Create a new dataset in the system.

    Creates a new dataset record with appropriate access controls. The dataset creator
    automatically receives read/write permissions, along with admin and read-only groups.
    If the dataset specifies an owner, project, or instrument, appropriate access groups
    are also granted permissions.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dataset: Dataset object containing all dataset metadata

    Returns:
        Dataset: The created dataset object with all fields populated

    Raises:
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if dataset data is invalid

    Access Control:
        - Admin group: read/write access
        - Read-only group: read access
        - Dataset owner: read/write access
        - Project group (if specified): read/write access
        - Instrument group (if specified): read/write access
        - Creator (if no owner specified): read/write access
    """
    
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds = add_or_update(session,
                           Dataset,
                           Dataset.unique_id == dataset.unique_id,
                           **dataset.model_dump(exclude_unset = True))
        session.commit()
        session.refresh(ds)
        
        # set appropriate access controls
        admin = session.exec(select(AccessGroup).where(AccessGroup.group_name == "admin")).one_or_none()
        admin_acl = sql_create_ds_acl(
            admin, ds, read=True, write=True, session=session
        )

        ro = session.exec(select(AccessGroup).where(AccessGroup.group_name == "read_only")).one_or_none()
        ro_acl = sql_create_ds_acl(
            ro, ds, read=True, write=False, session=session
        )
        print("add ro access")

        """
        All this logic is so backward because we literally
        have the instrument name and orcid then we do get
        requests to get the "IDs" now we have to do the get
        requests AGAIN to get the information we actually want
        and had the whole time
        """
        
        if dataset.owner_user_id is not None:
            user_orcid = session.exec(
                select(User.orcid).where(User.id == dataset.owner_user_id)
            ).one_or_none()
            user_ag = session.exec(
                select(AccessGroup).where(AccessGroup.group_name == user_orcid)
            ).one_or_none()
            user_acl = sql_create_ds_acl(
                user_ag, ds, read=True, write=True, session=session
            )

        else:
            # use the apikey associated accessgroup as owner
            creator = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).one_or_none()
            creator_acl = sql_create_ds_acl(creator, ds, read = True, write = True, session=session)

            if is_orcid(access_group_name):
                ds.owner_orcid = access_group_name
                creator_user_id = session.exec(select(User.id).where(User.orcid == dataset.owner_orcid)).one_or_none()
                ds.owner_user_id = creator_user_id

        if dataset.project_id is not None:
            print('adding proposal access')
            prop_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == dataset.project_id)).one_or_none()
            prop_acl = sql_create_ds_acl(prop_ag, ds, read = True, write = True, session=session)
            print('added proposal access')

        if dataset.instrument_id is not None:
            print('adding instrument group access')
            inst_query = select(Instrument.instrument_name).where(Instrument.id == dataset.instrument_id)
            inst_name = session.exec(inst_query).one_or_none()

            ag_query = select(AccessGroup).where(AccessGroup.group_name == inst_name.lower())
            inst_ag = session.exec(ag_query).one_or_none()
            if inst_ag is not None:
                inst_acl = sql_create_ds_acl(inst_ag,
                                             ds,
                                             read = True,
                                             write = True,
                                             session=session) 
            else:
                inst_ag = add_or_update(session,
                                        AccessGroup,
                                        AccessGroup.group_name == inst_name.lower(),
                                        group_name = inst_name.lower())

                inst_acl = sql_create_ds_acl(inst_ag,
                                             ds,
                                             read = True,
                                             write = True,
                                             session=session)
            print('added instrument group access')

        add_or_update(session,
                      IdType,
                      where=(IdType.unique_id == dataset.unique_id),
                      unique_id = dataset.unique_id,
                      object_type = "dataset")
        
        print('added the ID type')
        session.commit()
        print('committed')
        session.refresh(ds)

    return ds


@app.patch("/datasets/{dsid}", response_model=Dataset)
def update_dataset(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str,
    updated_dataset: Dataset
):
    """Update an existing dataset with new metadata.

    Updates dataset fields with provided values and automatically manages access controls
    for any newly specified owner, project, or instrument. Only fields provided in the
    request body will be updated (partial updates supported).

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset to update
        updated_dataset: Dataset object containing fields to update

    Returns:
        Dataset: The updated dataset object with all current field values

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if update data is invalid

    Access Control:
        - User must have write access to the dataset
        - If owner_user_id is updated, new owner gets read/write access
        - If project_id is updated, project group gets read/write access
        - If instrument_id is updated, instrument group gets read/write access

    Note:
        Sends notification to RabbitMQ when updates are completed.
    """
    access_group_name, ag_ids = auth_res
    #newds = table_obj_to_dict(updated_dataset)
    ds_cmd = select(Dataset).select_from(
                                join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(
                                    Dataset.unique_id == dsid).filter(
                                        DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=404,
                            detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=404,
                            detail="Dataset not found")
    else:
        pass
    

    with Session(engine) as session:
        stored_ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if stored_ds:   
            stored_ds_dict = dict(stored_ds)
        print(f"{stored_ds_dict=}")

        update_attr = updated_dataset.model_dump(exclude_unset=True)
        print(f"{update_attr=}")
        filtered_updates = {k:v for k,v in update_attr.items()} # I think this got solved in the io instead..

        
        upds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **filtered_updates)


        if upds.owner_user_id is not None:
            user_orcid = session.exec(select(User.orcid).where(User.id == upds.owner_user_id)).one_or_none()
            user_ag = session.exec(
                select(AccessGroup).where(AccessGroup.group_name == user_orcid)
            ).one_or_none()
            user_acl = sql_create_ds_acl(user_ag, upds, read = True, write = True, session=session)
            print('add owner orcid access')

        if upds.project_id is not None:
            prop_name = session.exec(select(Project.project_id).where(Project.project_id == upds.project_id)).one_or_none()
            prop_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == prop_name)).one_or_none()
            prop_acl = sql_create_ds_acl(prop_ag, upds, read = True, write = True, session=session)
            print('add proposal access')

        if upds.instrument_id is not None:
            inst_name = session.exec(select(Instrument.instrument_name).where(Instrument.id == upds.instrument_id)).one_or_none()
            inst_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == inst_name)).one_or_none()
            inst_acl = sql_create_ds_acl(inst_ag, upds, read = True, write = True, session=session) 
            print('add instrument group access')
        
        session.commit()
        session.refresh(upds)
        return upds


@app.get("/datasets", response_model=List[Dataset])
def list_datasets(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    request: Request
):
    """List datasets accessible to the authenticated user with optional filtering.

    Returns datasets that the user has access to through their access groups or public
    datasets. Supports filtering by dataset attributes, keywords, and scientific metadata
    through query parameters.

    Args:
        auth_res: Authorization result containing access group name and IDs
        request: FastAPI Request object containing query parameters

    Returns:
        List[Dataset]: List of dataset objects matching the filter criteria

    Raises:
        HTTPException: 401/403 if user is not authorized

    Query Parameters:
        - keyword: Filter by keyword (case-insensitive partial match)
        - {dataset_field}: Filter by any Dataset model field (exact match)
        - {metadata_key}: Filter by scientific metadata using double underscore notation
          (e.g., "temperature__value=25.5" for nested JSON filtering)

    Access Control:
        - Returns datasets user has access to via access groups
        - Includes public datasets regardless of access groups
        - No results returned if user has no permissions

    Examples:
        - GET /datasets - All accessible datasets
        - GET /datasets?keyword=electron - Datasets with "electron" keyword
        - GET /datasets?owner_orcid=0000-0000-0000-0001 - By owner
        - GET /datasets?temperature=298.15 - By scientific metadata field

    Note:
        Scientific metadata filtering supports nested JSON paths using double
        underscores to separate levels (e.g., "instrument__voltage=200").
    """
    access_group_name, ag_ids = auth_res
    
    query_params = request.query_params
    params_dict = dict(query_params)
    
    if 'keyword' in list(params_dict.keys()):
        v = params_dict['keyword']
        with Session(engine) as session:
            K = session.exec(select(Keyword).where(Keyword.keyword.ilike(f"%{v}%"))).all()
            
        if K:
            k_ids = [k.id for k in K]
            ds_cmd = select(Dataset).select_from(join(DatasetKeywordLink,
                                                 join(Dataset, DatasetACL, 
                                                      DatasetACL.dataset_id == Dataset.id, isouter=True),
                                                      DatasetKeywordLink.dataset_id == Dataset.id)).filter(
                                                      (DatasetACL.accessgroup_id.in_(ag_ids)) | 
                                                      (Dataset.public == True)).filter(
                                                      DatasetKeywordLink.keyword_id.in_(k_ids))
        
        del params_dict['keyword']

        
    else:
        ds_cmd = select(Dataset).select_from(
                                                join(Dataset, 
                                                     DatasetACL,
                                                     DatasetACL.dataset_id == Dataset.id, isouter=True)
                                                ).filter(
                                                    (DatasetACL.accessgroup_id.in_(ag_ids)) | 
                                                    (Dataset.public == True)
                                                )
    
    ds_cmd = ds_cmd.join(ScientificMetadata, ScientificMetadata.dataset_unique_id == Dataset.unique_id, isouter=True)


    for k, v in params_dict.items():
        if hasattr(Dataset, k):
            ds_cmd = ds_cmd.where(getattr(Dataset, k) == v)
        else:
            # Support nested JSON keys with double underscores
            json_path = k.split('__')
            json_col = ScientificMetadata.scientific_metadata
            for key in json_path[:-1]:
                json_col = json_col.op('->')(key)
            json_col = json_col.op('->>')(json_path[-1])
            print(json_col)
            ds_cmd = ds_cmd.where(json_col == v)
            
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd.distinct()).all()
        return ds_list
    
    
@app.get("/datasets/{dsid}", response_model=Union[Dataset, None])
def read_dataset(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str
):
    """Retrieve a specific dataset by its unique identifier.

    Returns the complete dataset record including all metadata fields if the user
    has access permissions. Returns None if the dataset doesn't exist or the user
    lacks access permissions.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset to retrieve

    Returns:
        Dataset | None: The dataset object if found and accessible, None otherwise

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset through access groups
        - Public datasets are accessible to all authenticated users
        - Returns None (not 404) if dataset exists but user lacks access

    Response Content:
        Returns the complete Dataset model with all fields including:
        - Basic metadata (name, description, creation time, etc.)
        - Ownership information (owner_orcid, owner_user_id)
        - Project and instrument associations
        - File upload information
        - Public/private status

    Note:
        This endpoint does not include related objects like scientific metadata,
        thumbnails, or associated files. Use specific sub-endpoints for those.
    """
    access_group_name, ag_ids = auth_res
    print(f"{ag_ids=}")
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        return ds



@app.delete("/datasets/{dsid}", response_model=Dataset)
def delete_dataset(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str
):
    """Delete a dataset.

    TODO: DEFINE THIS
    """
    access_group_name, ag_ids = auth_res
    pass



@app.get("/datasets/{dsid}/download/{file_name}")
async def download_dataset_files(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str,
    file_name: str
):
    """Download a file associated with a dataset directly from storage.

    Provides direct file download access from mounted GCS storage without requiring
    file copying or staging. Files are served with appropriate headers for browser
    download functionality.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset containing the file
        file_name: Relative path/name of the file to download

    Returns:
        FileResponse: Direct file stream with appropriate download headers

    Raises:
        HTTPException: 404 if dataset not found, user lacks access, or file not found
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Public datasets are accessible to all authenticated users

    File Path Resolution:
        - Files are located at: /mnt/mf-storage-prod/{dsid}/{file_name}
        - file_name can include subdirectory paths (e.g., "data/results.csv")
        - Path traversal attacks are prevented by storage mount restrictions

    Response Headers:
        - Content-Type: application/octet-stream (generic binary)
        - Content-Disposition: attachment with original filename
        - File size and other metadata included automatically

    Note:
        This provides zero-copy file access by serving directly from mounted
        storage, making it efficient for large files. File availability depends
        on successful dataset ingestion and storage mounting.
    """
    access_group_name, ag_ids = auth_res
    print("got auth")
    
    # check dataset access
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, 
                                              DatasetACL.dataset_id == Dataset.id, isouter=True)
                                        ).where(Dataset.unique_id == dsid
                                               ).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

    if not ds:
        raise HTTPException(status_code=404, detail="Dataset not found or access denied.")

    # Use mounted GCS storage directly - no copying needed
    file_path = f"/mnt/mf-storage-prod/{dsid}/{file_name}"
    
    if os.path.exists(file_path):
        return FileResponse(file_path, media_type="application/octet-stream", filename=file_name)
    else:
        raise HTTPException(status_code=404, detail="File not found for requested dataset ID.")


@app.post("/datasets/{dsid}/upload", response_model = IngestionRequest)
async def create_dataset_from_file_upload(files: Annotated[List[UploadFile], File(description="List of files")],
                                          auth_res:  Annotated[tuple, Depends(authorize_user)],
                                          dsid: str):
    """Upload files to an existing dataset for processing.

    Uploads one or more files to the dataset storage location and prepares them for
    ingestion. Files are stored in the GCS api-uploads directory and the first file
    is marked as the primary file for ingestion.

    Args:
        files: List of files to upload (multipart/form-data)
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the target dataset

    Returns:
        IngestionRequest: The dataset record with file_to_upload field set

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 413 if files are too large

    Process:
        1. Validates user has access to the dataset
        2. Creates necessary directory structure in GCS
        3. Saves all uploaded files to api-uploads/{filename}
        4. Sets the first file as the primary file for ingestion
        5. Updates the dataset record with file path

    Note:
        This endpoint prepares files for ingestion but does not start the
        ingestion process. Use /datasets/{dsid}/ingest to begin processing.
    """
    
    access_group_name, ag_ids = auth_res
    print(dsid)
    print(files)
    # upload the file
    for file in files:
        dest_dir = f"/mnt/gcs/api-uploads/{os.path.dirname(file.filename)}"
            
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        save_file_path = f"/mnt/gcs/api-uploads/{file.filename}"
        with open(save_file_path, "wb") as f:
            f.write(await file.read())

    main_file_for_ingestion = files[0].filename

        
    # get the dataset
    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        ds.file_to_upload = os.path.join("api-uploads", main_file_for_ingestion)
        session.add(ds)
        session.commit()
        session.refresh(ds)
    return(ds)


@app.post("/datasets/{dsid}/ingest", response_model = IngestionRequest)
def request_dataset_ingestion(auth_res: Annotated[tuple, Depends(authorize_user)],
                              dsid: str,
                              file_to_upload: Optional[str]=None,
                              ingestion_class: Optional[str]=None):
    """Request ingestion processing for a dataset's uploaded files.

    Creates an ingestion request to process uploaded files and extract metadata.
    The request is queued in RabbitMQ for background processing by ingestion workers.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset to ingest
        file_to_upload: Optional path to specific file (uses dataset's file_to_upload if not provided)
        ingestion_class: Optional ingestion class for specialized processing

    Returns:
        IngestionRequest: The created ingestion request with status "requested"

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 400 if no file is available for ingestion
        HTTPException: 401/403 if user is not authorized

    Process:
        1. Validates user has write access to the dataset
        2. Determines the file to process (parameter or dataset's file_to_upload)
        3. Creates an IngestionRequest record with "requested" status
        4. Sends processing message to RabbitMQ "ingest-newapi" queue
        5. Returns the request object for tracking

    Note:
        The actual ingestion processing happens asynchronously in background workers.
        Use GET /datasets/{dsid}/ingest/{reqid} to check processing status.
    """
    
    access_group_name, ag_ids = auth_res
    print("request received")
    # for this to work you should add the person who creates
    # the dataset ID to the access groups when they create the dataset
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(
                                        Dataset.unique_id == dsid).filter(
                                        DatasetACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

    if ds is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    
    elif file_to_upload is None:
        file_to_upload = ds.file_to_upload

    else:
        ds.file_to_upload = file_to_upload

    if not file_to_upload:
        raise HTTPException(status_code=400, detail="No file listed to ingest")

    pst= pytz.timezone("America/Los_Angeles")
    creation_time = get_tz_isoformat()
    ingestion_request = IngestionRequest(dataset_id=dsid,
                                         time_created=creation_time,
                                         file_to_upload=file_to_upload,
                                         status="requested")
    with Session(engine) as session:
        session.add(ingestion_request)
        session.commit()
        session.refresh(ingestion_request)
        print('committed')
        
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    msg = { "filename": file_to_upload,
            "reqid": ingestion_request.id,
            "dsid": dsid, 
            "ingestion_class":ingestion_class}
    
    send_pika_message(message = json.dumps(msg),
                      exchange= "",
                      routing_key= "ingest-newapi",
                      user = 'admin',
                      rmq_pw= rmq_pw)
    
    return ingestion_request


@app.get("/datasets/{dsid}/scicat_update/{reqid}", response_model = SciCatUpdateRequest)
def get_scicat_update_request(auth_res: Annotated[tuple, Depends(authorize_user)], reqid: int, dsid: str):
    """Retrieve the status and details of a SciCat synchronization request.

    Returns information about a specific SciCat update request including current
    status, timestamps, and any error messages from the synchronization process.

    Args:
        auth_res: Authorization result containing access group name and IDs
        reqid: Unique identifier of the SciCat update request
        dsid: Unique identifier of the dataset

    Returns:
        SciCatUpdateRequest: The SciCat update request with current status

    Raises:
        HTTPException: 404 if dataset not found, user lacks access, or request not found
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Request must belong to the specified dataset

    Status Values:
        - "requested": Request created, waiting for processing
        - "in_progress": Currently being synchronized
        - "completed": Successfully synchronized to SciCat
        - "failed": Synchronization failed (check error details)

    Note:
        Use this endpoint to monitor the progress of SciCat synchronization
        requests initiated via POST /datasets/{dsid}/scicat_update.
    """

    access_group_name, ag_ids = auth_res
    
    # Check dataset access first
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(
        Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()
        
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")
        
        # Get the scicat update request
        sc_req = session.exec(select(SciCatUpdateRequest).where(
            (SciCatUpdateRequest.id == reqid) & (SciCatUpdateRequest.dataset_id == dsid)
        )).one_or_none()
        
        if sc_req is None:
            raise HTTPException(status_code=404, detail="Scicat Update request not found")
        
        return sc_req
        

@app.get("/datasets/{dsid}/ingest/{reqid}", response_model = IngestionRequest)
def get_ingestion_request(auth_res: Annotated[tuple, Depends(authorize_user)], reqid: int, dsid: str):
    """Retrieve the status and details of a dataset ingestion request.

    Returns information about a specific ingestion request including current status,
    file paths, processing timestamps, and any error messages from the ingestion process.

    Args:
        auth_res: Authorization result containing access group name and IDs
        reqid: Unique identifier of the ingestion request
        dsid: Unique identifier of the dataset

    Returns:
        IngestionRequest: The ingestion request with current status and details

    Raises:
        HTTPException: 404 if dataset not found, user lacks access, or request not found
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Request must belong to the specified dataset

    Status Values:
        - "requested": Request created, queued for processing
        - "in_progress": File is currently being processed
        - "completed": Ingestion completed successfully
        - "failed": Processing failed (check error details)

    Response Details:
        - file_to_upload: Path to the file being processed
        - time_created: When the request was initiated
        - status: Current processing status
        - Additional processing metadata as available

    Note:
        Use this endpoint to monitor ingestion progress for requests initiated
        via POST /datasets/{dsid}/ingest.
    """
    access_group_name, ag_ids = auth_res
    
    # Check dataset access first
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(
        Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()
        
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")
        
        # Get the ingestion request
        ing_req = session.exec(select(IngestionRequest).where(
            (IngestionRequest.id == reqid) & (IngestionRequest.dataset_id == dsid)
        )).one_or_none()
        
        if ing_req is None:
            raise HTTPException(status_code=404, detail="Ingestion request not found")
        
        return ing_req


@app.patch("/datasets/{dsid}/ingest/{reqid}", response_model = IngestionRequest)
def patch_ingestion_request(is_admin: Annotated[str, Depends(is_admin)], reqid: int, dsid: str, ing_req: IngestionRequest):
    """Update the status and details of an ingestion request (Admin only).

    Updates an existing ingestion request with new status, error messages, or other
    processing details. This endpoint is typically used by ingestion workers to
    report processing progress and results.

    Args:
        is_admin: Admin authorization dependency
        reqid: Unique identifier of the ingestion request
        dsid: Unique identifier of the dataset
        ing_req: IngestionRequest object with updated fields

    Returns:
        IngestionRequest: The updated ingestion request object

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 400 if request ID mismatch or request not found
        HTTPException: 422 if update data is invalid

    Access Control:
        - Admin access required

    Note:
        The request ID in the URL must match the ID in the request body if provided.
        Only specified fields in the request body will be updated.
    """
    if ing_req.id:
        print(reqid)
        print(ing_req.id)
        assert reqid == ing_req.id
        
    with Session(engine) as session:
        updated_req = only_update(session,
                                  IngestionRequest,
                                  (IngestionRequest.id == ing_req.id) & (IngestionRequest.dataset_id == dsid), 
                                  **ing_req.model_dump(exclude_unset=True))
        if updated_req is None:
            errmsg = "No ingestion request found for the \
                      provided dataset and request ID combination"
            
            raise HTTPException(status_code=400,
                                detail=errmsg)
        
        session.commit()
        session.refresh(updated_req)
        
    return(updated_req)


@app.post("/datasets/{dsid}/google_drive_transfer", response_model = GoogleDriveTransferRequest)
def request_dataset_transfer_to_drive(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    """Request transfer of a dataset to Google Drive.

    Creates a transfer request to copy dataset files from GCS storage to Google Drive.
    The target drive is determined automatically based on the dataset's project and owner.
    Transfer requests are processed asynchronously by background workers.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset to transfer

    Returns:
        GoogleDriveTransferRequest: The created transfer request with status "requested"

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized

    Process:
        1. Validates user has access to the dataset
        2. Creates a GoogleDriveTransferRequest record with "requested" status
        3. Sends transfer message to RabbitMQ "transfer-newapi" queue
        4. Returns the request object for tracking

    Access Control:
        - User must have read access to the dataset

    Note:
        The actual transfer processing happens asynchronously in background workers.
        Use GET /datasets/{dsid}/google_drive_transfer/{reqid} to check transfer status.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        pst= pytz.timezone("America/Los_Angeles")
        creation_time = datetime.datetime.now(pst).isoformat()

        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
        with Session(engine) as session:
            ds_list = session.exec(ds_cmd).all()
    
        if ds_list is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        elif len(ds_list) == 0:
            raise HTTPException(status_code=404, detail="Dataset not found")
        else:
            pass

        transfer_request = GoogleDriveTransferRequest(dataset_id = dsid, time_created=creation_time, status = "requested")
        session.add(transfer_request)
        session.commit()
        session.refresh(transfer_request)

        message = json.dumps({"dsid":dsid, "source_bucket":"mf-storage-prod", "reqid":transfer_request.id})
        rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
        
        send_pika_message(message, exchange = "", routing_key= "transfer-newapi", user = "admin", rmq_pw = rmq_pw)
        return(transfer_request)


@app.patch("/datasets/{dsid}/google_drive_transfer/{reqid}", response_model = GoogleDriveTransferRequest)
def patch_transfer_request(is_admin: Annotated[str, Depends(is_admin)], reqid: int, dsid: str, xfer_req: GoogleDriveTransferRequest):
    """Update the status and details of a Google Drive transfer request (Admin only).

    Updates an existing transfer request with new status, drive location details, or error
    information. This endpoint is typically used by transfer workers to report processing
    progress and results.

    Args:
        is_admin: Admin authorization dependency
        reqid: Unique identifier of the transfer request
        dsid: Unique identifier of the dataset
        xfer_req: GoogleDriveTransferRequest object with updated fields

    Returns:
        GoogleDriveTransferRequest: The updated transfer request object

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 400 if request ID mismatch or request not found
        HTTPException: 422 if update data is invalid

    Access Control:
        - Admin access required

    Note:
        The request ID in the URL must match the ID in the request body if provided.
        Only specified fields in the request body will be updated.
    """
    if xfer_req.id:
        print(f"{reqid=}")
        print(f"{xfer_req.id=}")
        assert reqid == xfer_req.id
    with Session(engine) as session:
        updated_req = only_update(session, GoogleDriveTransferRequest, (GoogleDriveTransferRequest.id == xfer_req.id) & (GoogleDriveTransferRequest.dataset_id == dsid), **xfer_req.model_dump(exclude_unset=True))
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.post("/datasets/{dsid}/scicat_update", response_model = SciCatUpdateRequest)
def request_update_scicat_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, overwrite_data: bool = False):
    """Request synchronization of dataset metadata to SciCat data catalog.

    Creates a request to update or create dataset records in the SciCat scientific data
    catalog system. This ensures dataset metadata is synchronized across systems for
    discoverability and compliance.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset to sync
        overwrite_data: Whether to overwrite existing SciCat records (default: False)

    Returns:
        SciCatUpdateRequest: The created update request with status "requested"

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized

    Process:
        1. Validates user has access to the dataset
        2. Creates a SciCatUpdateRequest record with "requested" status
        3. Sends sync message to RabbitMQ "scicat-newapi" queue
        4. Returns the request object for tracking

    Access Control:
        - User must have read access to the dataset

    Note:
        The actual SciCat synchronization happens asynchronously in background workers.
        Use GET /datasets/{dsid}/scicat_update/{reqid} to check sync status.
    """
    pst= pytz.timezone("America/Los_Angeles")
    creation_time = datetime.datetime.now(pst).isoformat()
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=404, detail="Dataset not found")
    else:
        pass

    # Should add this to database table but it is okay for now
    scicat_request = SciCatUpdateRequest(dataset_id = dsid, time_created = creation_time, status = "requested")
    session.add(scicat_request)
    session.commit()
    session.refresh(scicat_request)

    message = json.dumps({"dsid": dsid, "source_bucket":"mf-storage-prod", "reqid": scicat_request.id, 'overwrite_data': overwrite_data})
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")

    send_pika_message(message, exchange = "", routing_key= "scicat-newapi", user = 'admin', rmq_pw= rmq_pw)
    #send_pika_message(message, exchange = "raw-data   -processing", routing_key= "#.scicat-request.#", user = 'admin', rmq_pw= rmq_pw)
    return(scicat_request)


@app.patch("/datasets/{dsid}/scicat_update/{reqid}", response_model = SciCatUpdateRequest)
def patch_scicat_request(is_admin: Annotated[str, Depends(is_admin)], reqid: int, dsid: str, sc_req: SciCatUpdateRequest):
    """Update the status and details of a SciCat synchronization request (Admin only).

    Updates an existing SciCat update request with new status, sync results, or error
    information. This endpoint is typically used by sync workers to report processing
    progress and results.

    Args:
        is_admin: Admin authorization dependency
        reqid: Unique identifier of the SciCat update request
        dsid: Unique identifier of the dataset
        sc_req: SciCatUpdateRequest object with updated fields

    Returns:
        SciCatUpdateRequest: The updated SciCat request object

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 400 if request ID mismatch or request not found
        HTTPException: 422 if update data is invalid

    Access Control:
        - Admin access required

    Note:
        The request ID in the URL must match the ID in the request body if provided.
        Only specified fields in the request body will be updated.
    """
    if sc_req.id:
        assert reqid == sc_req.id
    with Session(engine) as session:
        updated_req = only_update(session, SciCatUpdateRequest, (SciCatUpdateRequest.id == sc_req.id) & (SciCatUpdateRequest.dataset_id == dsid), **sc_req.model_dump(exclude_unset=True))
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.get("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def read_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    """Retrieve the scientific metadata associated with a dataset.

    Returns the complete scientific metadata record containing experimental parameters,
    instrument settings, environmental conditions, and other research-specific data
    stored as structured JSON.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset

    Returns:
        ScientificMetadata: The scientific metadata object with JSON data

    Raises:
        HTTPException: 404 if dataset not found, user lacks access, or no metadata exists
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Public datasets are accessible to all authenticated users

    Metadata Structure:
        Returns a ScientificMetadata object containing:
        - dataset_unique_id: Associated dataset identifier
        - scientific_metadata: JSON object with experimental data

    Common Metadata Fields:
        - Experimental conditions (temperature, pressure, etc.)
        - Instrument parameters (voltage, magnification, etc.)
        - Sample preparation details
        - Measurement protocols
        - Processing parameters

    Note:
        Returns None if no scientific metadata has been associated with the dataset.
        Metadata structure is flexible and varies by experiment type.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_md = session.exec(select(ScientificMetadata).where(ScientificMetadata.dataset_unique_id == ds.unique_id)).one_or_none()
        return ds_md


@app.post("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def add_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, scientific_md: dict):
    """Add or replace scientific metadata for a dataset.

    Creates or completely replaces the scientific metadata record for a dataset.
    Scientific metadata includes experimental parameters, instrument settings,
    environmental conditions, and other research-specific information.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset
        scientific_md: Dictionary containing scientific metadata key-value pairs

    Returns:
        ScientificMetadata: The created or updated metadata object

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if metadata format is invalid

    Access Control:
        - User must have write access to the dataset

    Metadata Format:
        The scientific_md dictionary can contain any JSON-serializable data structure
        with experimental parameters, measurements, conditions, etc.

    Example:
        ```json
        {
            "temperature": 298.15,
            "pressure": 101325,
            "instrument_settings": {
                "voltage": 200,
                "magnification": "100x"
            }
        }
        ```
    """
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, 
                                              DatasetACL,
                                              DatasetACL.dataset_id == Dataset.id)).where(
                                                  Dataset.unique_id == dsid).filter(
                                                        DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        md_obj = add_or_update(session, 
                               ScientificMetadata,
                               where=(ScientificMetadata.dataset_unique_id == dsid),
                               dataset_unique_id = dsid,
                               scientific_metadata = scientific_md)

        session.commit()
        session.refresh(md_obj)
        return(md_obj)


@app.patch("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def update_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, scientific_md_updates: dict):
    """Update specific fields in dataset scientific metadata.

    Performs partial updates to existing scientific metadata by merging provided
    key-value pairs with existing metadata. New keys are added, existing keys are
    updated, and unspecified keys remain unchanged.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset
        scientific_md_updates: Dictionary containing metadata fields to update/add

    Returns:
        ScientificMetadata: The updated metadata object with merged values

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if update format is invalid

    Access Control:
        - User must have write access to the dataset

    Update Behavior:
        - Merges updates with existing metadata at the top level
        - New keys are added to the metadata
        - Existing keys are overwritten with new values
        - Nested objects are replaced entirely (not merged recursively)
        - Creates new metadata record if none exists

    Example:
        Existing metadata: `{"temp": 25, "settings": {"voltage": 100}}`
        Update: `{"pressure": 101325, "settings": {"current": 5}}`
        Result: `{"temp": 25, "pressure": 101325, "settings": {"current": 5}}`
    """
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        stored_md = session.exec(select(ScientificMetadata).where(ScientificMetadata.dataset_unique_id == dsid)).one_or_none()
        
        if stored_md:
            md_dict = stored_md.scientific_metadata

        else:
            md_dict = {}

        # this doesn't allow for updating nested fields without providing the whole tree from the first level down. 
        # could make recursive?
        for k,v in scientific_md_updates.items():
            md_dict[k] = v

        md_obj = add_or_update(session, ScientificMetadata, where=(ScientificMetadata.dataset_unique_id == dsid), dataset_unique_id = dsid, scientific_metadata = md_dict)

        session.commit()
        session.refresh(md_obj)
        return(md_obj)


@app.get("/datasets/{dsid}/thumbnails", response_model = List[Thumbnail])
def read_thumbnails(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    """Retrieve all thumbnail images associated with a dataset.

    Returns a list of thumbnail objects containing base64-encoded images that provide
    visual previews of the dataset contents for quick identification and browsing.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset

    Returns:
        List[Thumbnail]: List of thumbnail objects with image data

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Public datasets are accessible to all authenticated users

    Thumbnail Object Fields:
        - thumbnail_name: Display name for the thumbnail
        - thumbnail_b64str: Base64-encoded image data
        - dataset_id: Associated dataset identifier

    Response:
        - Returns empty list if no thumbnails are associated
        - Multiple thumbnails per dataset are supported
        - Images are typically in PNG or JPEG format

    Note:
        Thumbnails are useful for dataset browsing interfaces and provide
        quick visual identification of dataset contents.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_thumbnails = session.exec(select(Thumbnail).where(Thumbnail.dataset_id == ds.id)).all()
        return ds_thumbnails


@app.post("/datasets/{dsid}/thumbnails", response_model = Thumbnail)
def add_thumbnail(auth_res: Annotated[tuple, Depends(authorize_user)],
                  dsid: str,
                  thumbnail: Thumbnail):
    """Add a thumbnail image to a dataset.

    Creates a thumbnail record and associates it with the specified dataset.
    Thumbnails provide visual previews of dataset contents for quick identification
    and browsing. If a thumbnail with the same base64 content already exists,
    it will be reused.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset
        thumbnail: Thumbnail object containing name and base64-encoded image data

    Returns:
        Thumbnail: The created or existing thumbnail object

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if thumbnail data is invalid

    Access Control:
        - User must have write access to the dataset

    Thumbnail Format:
        - thumbnail_name: Display name for the thumbnail
        - thumbnail_b64str: Base64-encoded image data (PNG, JPEG, etc.)

    Note:
        Thumbnails are deduplicated by content hash to save storage space.
        Multiple datasets can share the same thumbnail if they have identical content.
    """
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        tn_obj = add_or_update(session, Thumbnail, where=(Thumbnail.thumbnail_b64str == thumbnail.thumbnail_b64str),
                                thumbnail_name = thumbnail.thumbnail_name, 
                                thumbnail_b64str = thumbnail.thumbnail_b64str, 
                                dataset_id = ds.id)
        print('added tnobj')
        if not (tn_obj in ds.thumbnails):  
            print("adding tnobj to ds")
            ds.thumbnails.append(tn_obj)
            session.add(ds)
            print("added tnobj to ds")
        session.commit()
        print("committed session")
        session.refresh(tn_obj)
    
    return(tn_obj)


@app.get("/datasets/{dsid}/associated_files", response_model = List[AssociatedFile])
def read_associated_files(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    """Retrieve metadata for all files associated with a dataset.

    Returns a list of file metadata records describing all files that are part of
    or related to the dataset, including names, sizes, and content hashes for
    integrity verification.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset

    Returns:
        List[AssociatedFile]: List of file metadata objects

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Public datasets are accessible to all authenticated users

    File Metadata Fields:
        - filename: Name of the file (may include subdirectory paths)
        - size: File size in bytes
        - sha256_hash: SHA256 hash for content verification
        - dataset_id: Associated dataset identifier

    Response:
        - Returns empty list if no files are associated
        - Files may include raw data, processed results, documentation, etc.
        - Use GET /datasets/{dsid}/download/{file_name} to download actual files

    Note:
        This endpoint provides file inventory without transferring file content.
        Actual file downloads require separate download requests.
    """
    access_group_name, ag_ids = auth_res
    
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_files = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)).all()
        return ds_files


@app.post("/datasets/{dsid}/associated_files", response_model = AssociatedFile)
def add_associated_file(auth_res: Annotated[tuple, Depends(authorize_user)],
                        dsid: str,
                        associated_file: AssociatedFile):
    """Add an associated file record to a dataset.

    Registers a file as being associated with a dataset by creating an AssociatedFile
    record. This tracks file metadata including name, size, and content hash for
    data integrity verification and file management.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset
        associated_file: AssociatedFile object with file metadata

    Returns:
        AssociatedFile: The created or existing associated file object

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if file metadata is invalid

    Access Control:
        - User must have write access to the dataset

    File Metadata:
        - filename: Name of the file (including path if nested)
        - size: File size in bytes
        - sha256_hash: SHA256 hash for content verification

    Note:
        Files are deduplicated by SHA256 hash. If a file with the same hash
        already exists, the existing record will be associated with the dataset.
    """
    print("accepted args")
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        af_obj = add_or_update(session, AssociatedFile, where=(AssociatedFile.sha256_hash == associated_file.sha256_hash),
                                       filename = associated_file.filename, 
                                       size = associated_file.size,
                                       sha256_hash = associated_file.sha256_hash, 
                                       dataset_id = ds.id)
        print("added af_obj")
        if not (af_obj in ds.associated_files):  
            print("adding afobj to ds")
            ds.associated_files.append(af_obj)
            session.add(ds)
            print("added afobj to ds")
        session.commit()
        print("committed session")
        session.refresh(af_obj)
        return(af_obj)
 

@app.get("/datasets/{dsid}/keywords", response_model = List[Keyword])
def list_keywords_for_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    """Retrieve all keywords/tags associated with a dataset.

    Returns a list of keyword objects that have been associated with the dataset
    for categorization, search, and discovery purposes. Keywords help organize
    and filter datasets by topic or research area.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset

    Returns:
        List[Keyword]: List of keyword objects associated with the dataset

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Public datasets are accessible to all authenticated users

    Keyword Object Fields:
        - keyword: The text of the keyword/tag
        - num_datasets: Total number of datasets using this keyword
        - id: Unique identifier for the keyword

    Response:
        - Returns empty list if no keywords are associated
        - Keywords are shared across the system (same keyword can tag multiple datasets)
        - Useful for building tag clouds or category filters

    Note:
        Keywords are case-sensitive and should follow consistent naming conventions
        to maximize their utility for dataset discovery.
    """
    
    access_group_name, ag_ids = auth_res
    
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_keywords = session.exec(select(Keyword).select_from(join(Keyword, DatasetKeywordLink, DatasetKeywordLink.keyword_id == Keyword.id)).where(DatasetKeywordLink.dataset_id == ds.id)).all()
        return ds_keywords


@app.post("/datasets/{dsid}/keywords", response_model = Keyword)
def add_keyword(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, keyword: str):
    """Add a keyword tag to a dataset for categorization and discovery.

    Associates a keyword with the specified dataset to improve searchability and
    organization. Keywords are shared across datasets and help users find related
    data through tag-based filtering.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset
        keyword: String keyword/tag to associate with the dataset

    Returns:
        Keyword: The keyword object with updated usage count

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if keyword format is invalid

    Access Control:
        - User must have write access to the dataset

    Behavior:
        - Creates new keyword if it doesn't exist
        - Reuses existing keyword if already present in system
        - Updates keyword usage count across all datasets
        - Prevents duplicate associations with same dataset

    Note:
        Keywords are case-sensitive and should follow consistent naming conventions.
    """
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        kw_obj = add_or_update(session, Keyword, where=(Keyword.keyword == keyword), keyword=keyword)

        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        if not (kw_obj in ds.keywords):  
            ds.keywords.append(kw_obj)

        count = session.exec(select(func.count(DatasetKeywordLink.dataset_id))\
                            .where(DatasetKeywordLink.keyword_id == kw_obj.id)).one()
        
        kw_obj.num_datasets = count
        session.add(kw_obj)
        session.add(ds)
        session.commit()
        session.refresh(kw_obj)
        return(kw_obj)


@app.get("/datasets/{dsid}/drive_location", response_model = List[DataDriveLocation])
def read_dataset_drive_location(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    """Retrieve Google Drive storage locations for a dataset.

    Returns a list of Google Drive locations where the dataset files are currently
    stored, including drive IDs, folder locations, and status information for
    tracking data distribution across storage systems.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset

    Returns:
        List[DataDriveLocation]: List of Google Drive location objects

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Public datasets are accessible to all authenticated users

    Drive Location Fields:
        - drive_id: Google Drive identifier
        - drive_name: Human-readable drive name
        - folder_id: Specific folder within the drive
        - status: Current status (e.g., 'current', 'archived', 'pending')
        - Additional metadata as available

    Response:
        - Returns empty list if dataset is not stored in Google Drive
        - Datasets may have multiple drive locations (copies, backups)
        - Only returns locations with status='current' by default

    Note:
        This information is useful for data management and tracking dataset
        availability across different storage systems.
    """
    with Session(engine) as session:
        ds_locs = session.exec(select(DataDriveLocation).where(and_(DataDriveLocation.dataset_id == dsid, DataDriveLocation.status == 'current'))).all()
    return(ds_locs)


@app.post("/datasets/{dsid}/drive_location", response_model = DataDriveLocation)
def create_dataset_drive_location(auth_res:  Annotated[tuple, Depends(authorize_user)], dsid: str, drive_info: DataDriveLocation):
    """Add a Google Drive location record for a dataset.

    Records the location of dataset files within Google Drive storage,
    including drive ID, folder ID, and organizational metadata. This enables
    tracking of dataset distribution across different storage systems.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset
        drive_info: DataDriveLocation object with Google Drive details

    Returns:
        DataDriveLocation: The created drive location record

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if drive location data is invalid

    Access Control:
        - User must have write access to the dataset

    Drive Location Fields:
        - drive_id: Google Drive identifier
        - drive_name: Human-readable drive name
        - folder_id: Specific folder within the drive
        - status: Current status of the data location

    Note:
        Automatically creates or updates Google Drive registry entries.
    """
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=404, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=404, detail="Dataset not found")
    else:
        pass
    with Session(engine) as session:
        d_info = table_obj_to_dict(drive_info)
        add_or_update(session, GoogleDrives, GoogleDrives.drive_id == drive_info.drive_id, drive_id = drive_info.drive_id, drive_name = drive_info.drive_name)
        drive_loc = add_or_update(session, 
                      DataDriveLocation,
                      DataDriveLocation.folder_id == drive_info.folder_id, 
                      **d_info)
        session.commit()
        session.refresh(drive_loc)
    return(drive_loc)


@app.get("/datasets/{dsid}/access_groups", response_model = List[AccessGroup])
def read_dataset_acl(dsid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    """Retrieve all access groups with permissions to a dataset (Admin only).

    Returns a list of access groups that have been granted read and/or write
    permissions to the specified dataset. This provides visibility into the
    complete access control configuration for data governance.

    Args:
        dsid: Unique identifier of the dataset
        admin_acct: Admin authorization dependency

    Returns:
        List[AccessGroup]: List of access groups with dataset permissions

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 404 if dataset not found

    Access Control:
        - Admin access required
        - Regular users cannot view access control lists

    Access Group Fields:
        - group_name: Name of the access group
        - id: Unique identifier for the group
        - Additional group metadata

    Permission Types:
        Each group may have different permission levels:
        - Read access: Can view dataset and download files
        - Write access: Can modify dataset metadata and upload files
        - Combined permissions are stored in DatasetACL records

    Note:
        This endpoint shows which groups have access but not the specific
        permission levels. Use admin tools to view detailed ACL information.
    """
    with Session(engine) as session:
        dscmd = select(AccessGroup).select_from(join(AccessGroup, join(DatasetACL, 
                                                                     Dataset,
                                                                     DatasetACL.dataset_id == Dataset.id),
                                                    AccessGroup.id == DatasetACL.accessgroup_id))
        dscmd = dscmd.where(Dataset.unique_id == dsid)
        ds_acl = session.exec(dscmd).all()
        
        return ds_acl


def sql_create_ds_acl(ag, ds, read, write, session):
    acl = session.exec(select(DatasetACL).where(DatasetACL.accessgroup == ag).where(DatasetACL.dataset == ds)).one_or_none()
    print(f"{acl=}")
    if not acl:
        acl = DatasetACL(accessgroup_id = ag.id, dataset_id = ds.id, accessgroup=ag, dataset=ds)
        acl.read = read
        acl.write = write
        session.add(acl)
        session.commit()
        session.refresh(acl)
    return(acl)


@app.post("/datasets/{dsid}/access_groups", response_model = DatasetACL)
def create_dataset_acl(group_name: str, dsid: str, read: bool, write: bool, admin_acct: Annotated[str, Depends(is_admin)]):
    """Create or update access control permissions for a dataset (Admin only).

    Grants specific read and/or write permissions to an access group for the specified
    dataset. This allows fine-grained control over who can view and modify dataset
    metadata and associated files.

    Args:
        group_name: Name of the access group to grant permissions
        dsid: Unique identifier of the dataset
        read: Whether to grant read access to the group
        write: Whether to grant write access to the group
        admin_acct: Admin authorization dependency

    Returns:
        DatasetACL: The created or updated access control record

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 404 if dataset or access group not found
        HTTPException: 422 if permission parameters are invalid

    Access Control:
        - Admin access required

    Permission Levels:
        - read=True: Group can view dataset metadata and files
        - write=True: Group can modify dataset metadata and upload files
        - Both can be True for full access
        - Both False effectively removes access

    Note:
        Creates access group if it doesn't exist. Updates existing permissions
        if ACL record already exists for the group-dataset combination.
    """
    with Session(engine) as session:
        print(f"{dsid=}")
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()

        acl = sql_create_ds_acl(ag, ds, read, write, session)
    return(acl)


@app.post("/dataset_collections", response_model = DatasetCollection)
def create_dataset_collection(auth_res:  Annotated[tuple, Depends(authorize_user)], dc: DatasetCollection):
    """Create a new dataset collection for organizing related datasets.

    Creates a collection that can group multiple related datasets together for
    organizational, discovery, and management purposes. Collections provide a
    way to logically group datasets that belong to the same study, project phase,
    or research theme.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dc: DatasetCollection object containing collection metadata

    Returns:
        DatasetCollection: The created collection object with assigned ID

    Raises:
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if collection data is invalid

    Access Control:
        - User must be authenticated
        - Collection creation permissions based on user access

    Collection Fields:
        - unique_id: System-generated unique identifier
        - collection_name: Human-readable collection name
        - description: Purpose and contents of the collection
        - Additional metadata and organizational information

    Use Cases:
        - Grouping datasets from multi-part experiments
        - Organizing datasets by research project or grant
        - Creating thematic collections for data discovery
        - Managing related datasets across time series

    Note:
        Collections are organizational containers only. Dataset access
        permissions are still controlled individually through dataset ACLs.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        session.add(dc)
        session.commit()
        session.refresh(dc)
    return(dc)


@app.get("/dataset_collections/{dcid}", response_model = Union[None, DatasetCollection])
def read_dataset_collection(admin_acct: Annotated[str, Depends(is_admin)], dcid: str):
    """Retrieve a specific dataset collection by its identifier (Admin only).

    Returns the complete collection record including metadata and organizational
    information for the specified collection ID. Returns None if no collection
    is found with the given identifier.

    Args:
        admin_acct: Admin authorization dependency
        dcid: Unique identifier of the collection to retrieve

    Returns:
        DatasetCollection | None: Collection object if found, None if not found

    Raises:
        HTTPException: 403 if user is not admin

    Access Control:
        - Admin access required
        - Collection details visible to administrators only

    Collection Information:
        - Complete collection metadata and description
        - Organizational and categorization details
        - Creation and modification timestamps
        - Associated collection links and relationships

    Response:
        - Returns None (not 404) if collection doesn't exist
        - Includes all collection fields and metadata
        - Does not include linked datasets (use separate endpoints)

    Use Cases:
        - Administrative collection management
        - Collection detail verification
        - System maintenance and oversight

    Note:
        Collection membership (which datasets belong to the collection)
        is managed through separate dataset-collection link endpoints.
    """
    with Session(engine) as session:
        dc = session.exec(select(DatasetCollection).where(DatasetCollection.unique_id == dcid)).one_or_none()
    return(dc)


@app.get("/dataset_collections", response_model = List[DatasetCollection])
def list_dataset_collections(admin_acct: Annotated[str, Depends(is_admin)], request: Request):
    """List dataset collections with optional filtering (Admin only).

    Returns a list of dataset collections matching the specified filter criteria
    through query parameters. Supports filtering by any DatasetCollection model
    field for administrative oversight and management.

    Args:
        admin_acct: Admin authorization dependency
        request: FastAPI Request object containing query parameters

    Returns:
        List[DatasetCollection]: List of collection objects matching filter criteria

    Raises:
        HTTPException: 403 if user is not admin

    Access Control:
        - Admin access required
        - System-wide collection visibility

    Query Parameters:
        - collection_name: Filter by collection name
        - unique_id: Filter by collection identifier
        - Any other DatasetCollection model field (exact match)

    Filtering:
        - All filters use exact matching
        - Multiple parameters can be combined (AND logic)
        - Empty parameters are ignored

    Response:
        - Returns empty list if no collections match criteria
        - Includes all collection metadata
        - Collections returned in database order

    Examples:
        - GET /dataset_collections - All collections
        - GET /dataset_collections?collection_name=Study1 - Named collection

    Use Cases:
        - Administrative collection oversight
        - Collection inventory and management
        - Research organization review
        - System maintenance and cleanup

    Note:
        This provides administrative visibility into all dataset
        collections for system management purposes.
    """
    query_params = request.query_params
    params_dict = dict(query_params)

    ds_cmd = select(DatasetCollection)
    
    for k, v in params_dict.items():
        if hasattr(DatasetCollection, k):
            ds_cmd = ds_cmd.where(getattr(DatasetCollection, k) == v)

    with Session(engine) as session:
        dc_list = session.exec(ds_cmd).all()
        return dc_list


@app.post("/access_groups", response_model = AccessGroup)
def create_access_group(admin_acct: Annotated[str, Depends(is_admin)], ag: AccessGroup):
    """Create a new access group for permission management (Admin only).

    Creates a new access group that can be used to manage permissions for
    datasets, projects, and other resources. Access groups provide the
    foundation for role-based access control throughout the system.

    Args:
        admin_acct: Admin authorization dependency
        ag: AccessGroup object containing group information

    Returns:
        AccessGroup: The created access group object

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 422 if access group data is invalid

    Access Control:
        - Admin access required
        - Only administrators can create access groups

    Access Group Fields:
        - group_name: Unique name for the access group
        - description: Purpose and scope of the group
        - Additional metadata and configuration

    Group Types:
        - User-specific groups (typically ORCID-based)
        - Project-based groups (project collaboration)
        - Instrument-based groups (equipment access)
        - Administrative groups (system management)
        - Custom groups (special purposes)

    Automatic Creation:
        Many access groups are created automatically:
        - User registration creates personal groups
        - Project creation creates project groups
        - Instrument registration creates instrument groups

    Use Cases:
        - Custom permission structures
        - Special access requirements
        - Administrative group management
        - Complex collaboration scenarios

    Note:
        Access groups control permissions throughout the system.
        Group membership determines resource access for users.
    """
    with Session(engine) as session:
        session.add(ag)
        session.commit()
    return(ag)


@app.get("/access_groups/{group_name}", response_model = Union[None, AccessGroup])
def read_access_group(admin_acct: Annotated[str, Depends(is_admin)], group_name: str):
    """Retrieve a specific access group by name (Admin only).

    Returns the complete access group record including metadata, membership
    information, and configuration details for the specified group name.
    Returns None if no group is found with the given name.

    Args:
        admin_acct: Admin authorization dependency
        group_name: Name of the access group to retrieve

    Returns:
        AccessGroup | None: Access group object if found, None if not found

    Raises:
        HTTPException: 403 if user is not admin

    Access Control:
        - Admin access required
        - Full access group details visible to administrators

    Group Information:
        - Complete group metadata and configuration
        - Group purpose and scope information
        - Creation and modification timestamps
        - Associated users and permissions

    Response:
        - Returns None (not 404) if group doesn't exist
        - Includes all group fields and metadata
        - May include membership information depending on implementation

    Common Group Names:
        - admin: System administrators
        - read_only: Read-only access
        - {orcid}: User-specific groups
        - {project_id}: Project-based groups
        - {instrument_name}: Instrument-based groups

    Use Cases:
        - Administrative group management
        - Permission verification and debugging
        - Group configuration review
        - Access control auditing

    Note:
        Group names are unique identifiers used throughout
        the system for permission management.
    """
    with Session(engine) as session:
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        return(ag)


@app.post("/users/{orcid}/access_groups/{group_name}", response_model = AccessGroup)
def add_user_to_access_group(admin_acct: Annotated[str, Depends(is_admin)], orcid: str, group_name: str):
    """Add a user to an access group (Admin only).

    Grants a user membership in the specified access group, which provides them
    with permissions to datasets and resources associated with that group.
    Creates the access group if it doesn't already exist.

    Args:
        admin_acct: Admin authorization dependency
        orcid: ORCID identifier of the user to add
        group_name: Name of the access group

    Returns:
        AccessGroup: The access group object with updated membership

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 404 if user with specified ORCID not found
        HTTPException: 422 if ORCID or group name format is invalid

    Access Control:
        - Admin access required
        - User must exist in the system before being added to groups

    Behavior:
        - Creates access group if it doesn't exist
        - Prevents duplicate memberships (idempotent operation)
        - Updates group membership list
        - User gains access to all resources associated with the group

    Note:
        Access groups control permissions to datasets, projects, and other resources.
        Group membership is persistent until explicitly removed.
    """
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        ag = add_or_update(session, 
                            AccessGroup,
                            AccessGroup.group_name == group_name,
                            group_name = group_name )
        
        if not (user in ag.users):
            ag.users.append(user)

        session.commit()
        session.refresh(ag)
    return(ag)


@app.get("/users/{orcid}/access_groups", response_model = List[str])
def list_user_access_groups(auth_res:  Annotated[tuple, Depends(authorize_user)], orcid: str):
    """Retrieve the access groups that a user belongs to.

    Returns a list of access group names that the specified user is a member of.
    This shows which permissions and resource access the user has been granted
    through group membership.

    Args:
        auth_res: Authorization result containing access group name and IDs
        orcid: ORCID identifier of the user to query

    Returns:
        List[str]: List of access group names the user belongs to

    Raises:
        HTTPException: 401/403 if user is not authorized
        HTTPException: 404 if user with specified ORCID not found

    Access Control:
        - User must be authenticated
        - Returns groups for the requesting user's access groups only
        - Users can only see their own group memberships

    Response:
        - Returns empty list if user belongs to no groups
        - Group names are returned as strings
        - Includes all active group memberships

    Use Cases:
        - Checking user permissions and access levels
        - Determining which datasets/projects user can access
        - User profile and authorization information

    Note:
        Access group membership determines what resources a user can
        view and modify throughout the system.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ags = session.exec(select(AccessGroup).where(AccessGroup.id).in_(ag_ids)).all()
        return([ag.group_name for ag in ags])



# =====================

@app.get("/keywords/{keyword}", response_model = List[Keyword])
def read_keyword(keyword: str, permissive: bool = True):
    """Search for keywords matching the provided search term.

    Returns a list of keyword objects that match the search criteria, with support
    for both exact matching and case-insensitive partial matching. Useful for
    keyword discovery, validation, and autocomplete functionality.

    Args:
        keyword: Search term to match against existing keywords
        permissive: Whether to use case-insensitive partial matching (default: True)

    Returns:
        List[Keyword]: List of keyword objects matching the search criteria

    Query Parameters:
        - permissive (default: True): Controls matching behavior

    Matching Modes:
        - permissive=True: Case-insensitive partial matching (contains)
        - permissive=False: Exact string matching (case-sensitive)

    Keyword Object Fields:
        - keyword: The text of the keyword/tag
        - num_datasets: Number of datasets using this keyword
        - id: Unique identifier for the keyword

    Response:
        - Returns empty list if no keywords match
        - Partial matches when permissive=True
        - Exact matches only when permissive=False
        - Includes usage statistics for each keyword

    Examples:
        - GET /keywords/electron - Partial match for "electron"
        - GET /keywords/electron?permissive=false - Exact match only
        - GET /keywords/microscopy - Find microscopy-related keywords

    Use Cases:
        - Keyword search and discovery
        - Dataset tagging assistance
        - Autocomplete for keyword fields
        - Keyword validation and consistency checking
        - Popular keyword identification

    Note:
        The num_datasets field shows keyword popularity and usage
        across the system, helping with tag standardization.
    """
    if permissive:
        cmd = select(Keyword).where(Keyword.keyword.ilike(f"{keyword.lower()}"))
    else:
        cmd = select(Keyword).where(Keyword.keyword == keyword)

    with Session(engine) as session:
        K = session.exec(cmd).all()
    return(K)


@app.get("/instruments", response_model = List[Instrument])
def list_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    """List scientific instruments with optional filtering by attributes.

    Returns a list of instruments registered in the system, with support for filtering
    by instrument characteristics through query parameters. All searches use exact
    matching but are case-insensitive for string fields.

    Args:
        auth_res: Authorization result containing access group name and IDs
        request: FastAPI Request object containing query parameters

    Returns:
        List[Instrument]: List of instrument objects matching filter criteria

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Returns all instruments visible to authenticated users

    Query Parameters (Exact Match, Case-Insensitive):
        - other_id: External identifier from other systems
        - other_id_source: Source system for external ID
        - instrument_name: Name of the instrument
        - manufacturer: Instrument manufacturer
        - model: Instrument model designation
        - owner: Institution or person owning the instrument
        - location: Physical location of the instrument
        - description: Instrument description or capabilities
        - instrument_type: Category or type of instrument

    Search Behavior:
        - All parameters are case-insensitive
        - Exact matching required (not partial/fuzzy)
        - Multiple parameters can be combined (AND logic)
        - Empty parameters are ignored

    Examples:
        - GET /instruments - All instruments
        - GET /instruments?manufacturer=zeiss - Zeiss instruments
        - GET /instruments?instrument_type=microscope&location=building1

    Note:
        Instrument registration enables dataset association and access control
        through instrument-based access groups.
    """
    access_group_name, ag_ids = auth_res
    query_params = request.query_params
    params_dict = dict(query_params)
    print(params_dict)
    statement = select(Instrument)
    for k,v, in params_dict.items():
        if hasattr(Instrument, k):
            print(f'has attr {k=}')
            statement = statement.where(getattr(Instrument, k) == v.lower().strip())
        else:
            print(f'does not have attr {k=}')
    with Session(engine) as session: 
        all_inst = session.exec(statement).all()

    return(all_inst)


@app.get("/instruments/{instrument_unique_id}", response_model = Union[None, Instrument])
def read_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], instrument_unique_id: str):
    """Retrieve a specific instrument by its unique identifier.

    Returns the complete instrument record including specifications, location,
    and metadata for the specified instrument ID. Returns None if no instrument
    is found with the given identifier.

    Args:
        auth_res: Authorization result containing access group name and IDs
        instrument_unique_id: Unique identifier of the instrument to retrieve

    Returns:
        Instrument | None: Instrument object if found, None if not found

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Returns instrument information if accessible

    Instrument Information:
        - Complete instrument specifications and metadata
        - Manufacturer, model, and technical details
        - Location and ownership information
        - Operating parameters and capabilities
        - External system identifiers

    Response:
        - Returns None (not 404) if instrument doesn't exist
        - Includes all instrument fields and specifications
        - Useful for dataset context and experimental setup

    Use Cases:
        - Instrument detail viewing
        - Experimental setup planning
        - Dataset context understanding
        - Equipment availability checking

    Note:
        Instruments are associated with datasets through instrument_id fields,
        enabling experiment-to-equipment traceability.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        inst = session.exec(select(Instrument).where(Instrument.unique_id == instrument_unique_id)).one_or_none()
        return(inst)


@app.post("/instruments", response_model = Instrument)
def create_instrument(auth_res:  Annotated[tuple, Depends(authorize_user)], instrument: Instrument):
    """Register a new scientific instrument in the system.

    Creates a new instrument record with complete specifications, location,
    and metadata information. The instrument name is automatically converted
    to lowercase for consistency in access group naming.

    Args:
        auth_res: Authorization result containing access group name and IDs
        instrument: Instrument object containing instrument specifications

    Returns:
        Instrument: The created instrument object with assigned ID

    Raises:
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if instrument data is invalid

    Access Control:
        - User must be authenticated
        - User registration confirms access to instrument creation

    Instrument Fields:
        - unique_id: System-generated unique identifier
        - instrument_name: Name (converted to lowercase)
        - manufacturer, model: Equipment specifications
        - owner: Institution or person responsible
        - location: Physical location of instrument
        - description: Capabilities and characteristics
        - instrument_type: Category classification
        - other_id, other_id_source: External system identifiers

    Automatic Processing:
        - Instrument name normalized to lowercase
        - Unique ID assigned during creation
        - Enables future access group creation

    Use Cases:
        - Equipment registration and inventory
        - Dataset-instrument association setup
        - Facility management and tracking
        - Research capability documentation

    Note:
        Instrument registration enables dataset association and access control.
        Lowercase naming ensures consistent access group naming conventions.
    """
    access_group_name, ag_ids = auth_res
    instrument.instrument_name = instrument.instrument_name.lower()
    with Session(engine) as session:
        session.add(instrument)
        session.commit()
        session.refresh(instrument)
    return(instrument)


@app.get("/google_drives", response_model = List[GoogleDrives])
def list_google_drive_info(auth_res:  Annotated[tuple, Depends(authorize_user)], request: Request):
    """List Google Drive storage systems with optional filtering.

    Returns a list of Google Drive storage systems registered in the system,
    with support for filtering by drive characteristics through query parameters.
    Useful for data management and storage system discovery.

    Args:
        auth_res: Authorization result containing access group name and IDs
        request: FastAPI Request object containing query parameters

    Returns:
        List[GoogleDrives]: List of Google Drive objects matching filter criteria

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Returns drive information visible to authenticated users

    Query Parameters:
        - drive_id: Filter by Google Drive identifier
        - drive_name: Filter by drive name
        - Any other GoogleDrives model field (exact match)

    Filtering:
        - All filters use exact matching
        - Multiple parameters can be combined (AND logic)
        - Empty parameters are ignored

    Drive Information:
        - Google Drive identifiers and names
        - Storage capacity and usage information
        - Access permissions and sharing settings
        - Integration configuration details

    Examples:
        - GET /google_drives - All registered drives
        - GET /google_drives?drive_name=Research_Data - Specific drive

    Use Cases:
        - Storage system discovery
        - Data transfer planning
        - Drive capacity monitoring
        - Integration configuration review

    Note:
        This provides visibility into available Google Drive storage
        systems for data management and transfer operations.
    """
    query_params = request.query_params
    params_dict = dict(query_params)

    search_cmd = select(GoogleDrives)

    for k, v in params_dict.items():
        if hasattr(GoogleDrives, k):
            search_cmd = search_cmd.where(getattr(GoogleDrives, k) == v)

    with Session(engine) as session:
        drives = session.exec(search_cmd).all()

    return(drives)


@app.get("/google_drives/{drive_name}", response_model = Union[None, GoogleDrives])
def read_google_drive_info(auth_res:  Annotated[tuple, Depends(authorize_user)], drive_name: str):
    """Retrieve specific Google Drive information by drive name.

    Returns the complete Google Drive record including configuration, capacity,
    and integration details for the specified drive name. Returns None if no
    drive is found with the given name.

    Args:
        auth_res: Authorization result containing access group name and IDs
        drive_name: Name of the Google Drive to retrieve

    Returns:
        GoogleDrives | None: Google Drive object if found, None if not found

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Drive information visible to authenticated users

    Drive Information:
        - Complete drive configuration and metadata
        - Storage capacity and usage statistics
        - Access permissions and sharing configuration
        - Integration settings and API details

    Response:
        - Returns None (not 404) if drive doesn't exist
        - Includes all drive fields and configuration
        - Useful for transfer planning and storage management

    Use Cases:
        - Drive configuration verification
        - Storage capacity checking
        - Transfer destination validation
        - Integration troubleshooting

    Note:
        Drive names are human-readable identifiers used for
        storage system identification and data transfer operations.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        drive = session.exec(select(GoogleDrives).where(GoogleDrives.drive_name == drive_name)).all()

    return(drive)


@app.post("/google_drives", response_model = GoogleDrives)
def create_google_drive_info(auth_res:  Annotated[tuple, Depends(authorize_user)], D: Dict):
    """Register or update Google Drive storage system information.

    Creates a new Google Drive record or updates an existing one with storage
    configuration, capacity, and integration details. This enables the system
    to manage data transfers and storage operations across multiple drives.

    Args:
        auth_res: Authorization result containing access group name and IDs
        D: Dictionary containing Google Drive information and configuration

    Returns:
        GoogleDrives: The created or updated Google Drive object

    Raises:
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if drive data is invalid

    Access Control:
        - User must be authenticated
        - Drive registration permissions based on user access

    Drive Configuration Fields:
        - drive_id: Unique Google Drive identifier
        - drive_name: Human-readable drive name
        - Storage capacity and usage information
        - Access permissions and sharing settings
        - API configuration and integration details

    Update Behavior:
        - Creates new drive record if drive_id doesn't exist
        - Updates existing record if drive_id matches
        - Preserves existing configuration not specified in update

    Use Cases:
        - Storage system registration
        - Drive configuration updates
        - Capacity and usage tracking
        - Integration setup and maintenance

    Error Handling:
        - Invalid drive configurations are rejected
        - API errors are logged and handled gracefully
        - Duplicate drive_id updates existing record

    Note:
        Drive registration enables automated data transfer
        and storage management across Google Drive systems.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        if True:
            try:
                dr = add_or_update(session, 
                                   GoogleDrives, 
                                   GoogleDrives.drive_id == D['drive_id'], 
                                   **D)
                
                session.commit()
                session.refresh(dr)
            except Exception as err:
                print(err)

    return(D)

# =======================================
@app.get("/users", response_model = List[User])
def list_users(admin_acct:  Annotated[str, Depends(is_admin)], request: Request, permissive = True):
    """List users in the system with optional filtering (Admin only).

    Returns a list of users matching the specified filter criteria. Supports
    filtering by user attributes through query parameters with optional
    permissive (partial) or exact matching.

    Args:
        admin_acct: Admin authorization dependency
        request: FastAPI Request object containing query parameters
        permissive: Whether to use partial matching (default: True)

    Returns:
        List[User]: List of user objects matching the filter criteria

    Raises:
        HTTPException: 403 if user is not admin

    Access Control:
        - Admin access required
        - Regular users cannot list other users

    Query Parameters:
        - first_name: Filter by first name
        - last_name: Filter by last name
        - email: Filter by email address
        - lbl_email: Filter by LBL email address
        - orcid: Filter by ORCID identifier
        - Any other User model field

    Filtering Modes:
        - permissive=True: Case-insensitive partial matching (contains)
        - permissive=False: Exact matching (case-sensitive)

    Examples:
        - GET /users - All users
        - GET /users?first_name=John - Users with "John" in first name
        - GET /users?email=example.com - Users with "example.com" in email

    Note:
        This replaces separate endpoints for name and email filtering.
        Use specific field names rather than generic "name" parameter.
    """

    query_params = request.query_params
    params_dict = dict(query_params)
    search_cmd = select(User)

    # this replaces get_users_by_name and users_by_email ---- need to specify lbl_email or email in parameters now --- won't check both
    # need to specify first_name and last_name not just "name"

    if permissive:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k).ilike(f"%{v.lower()}%"))
               
    else:
        for k, v in params_dict.items():
            if hasattr(User, k):
                search_cmd = search_cmd.where(getattr(User, k) == v)
                
    with Session(engine) as session:
        users = session.exec(search_cmd).all()
        return users


@app.get("/users/{orcid}", response_model = Union[None, User])
def read_user(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    """Retrieve a specific user by their ORCID identifier (Admin only).

    Returns the complete user record including personal information, contact details,
    and system metadata for the specified ORCID. Returns None if no user is found
    with the given ORCID.

    Args:
        orcid: ORCID identifier of the user to retrieve
        admin_acct: Admin authorization dependency

    Returns:
        User | None: User object if found, None if not found

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 422 if ORCID format is invalid

    Access Control:
        - Admin access required
        - Regular users cannot view other user profiles

    User Object Fields:
        - orcid: ORCID identifier
        - first_name, last_name: Personal information
        - email, lbl_email: Contact information
        - Additional user metadata and timestamps

    Response:
        - Returns complete user profile if found
        - Returns None (not 404) if user doesn't exist
        - Includes all user fields and relationships

    Use Cases:
        - Admin user management
        - User profile lookup
        - System administration tasks

    Note:
        ORCID identifiers follow the format 0000-0000-0000-000X where
        the last character can be a digit or 'X'.
    """
    with Session(engine) as session:
        users = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        return users


@app.post("/users", response_model = User)
def create_user(admin_acct:  Annotated[str, Depends(is_admin)], user_info: User, project_ids: List[str] = Body(...)):
    """Create or update a user account with project associations (Admin only).

    Creates a new user account or updates an existing one, automatically setting up
    access groups and project associations. The user is added to access groups for
    their ORCID and any projects they participate in as a regular user.

    Args:
        admin_acct: Admin authorization dependency
        user_info: User object containing user information
        project_ids: List of project IDs where user is a participant (not lead)

    Returns:
        User: The created or updated user object

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 422 if user data or project IDs are invalid

    Access Control:
        - Admin access required
        - Creates access control automatically

    Automatic Setup:
        1. Creates/updates user record in database
        2. Creates access group named after user's ORCID
        3. Adds user to their personal access group
        4. Links user to specified projects as participant
        5. Adds user to access groups for each project

    Project Association:
        - User is added as PROJECT PARTICIPANT (not lead) for specified projects
        - Project leads are NOT automatically added to project access groups
        - Each project gets its own access group for permissions

    Access Groups Created:
        - Personal access group: {orcid}
        - Project access groups: {project_id}

    Note:
        This endpoint handles the complete user onboarding process including
        access control setup. Project leadership is handled separately.
    """
    with Session(engine) as session:
        if True:
            # add user to user table
            user_dict = table_obj_to_dict(user_info)
            user = add_or_update(session, User, User.orcid == user_info.orcid, **user_dict)
            session.commit()
            
            apikey = get_secret("ADMIN_APIKEY", "crucible_admin_apikey/versions/3")
            requests.request("post", url =  f"{api_url}/users/{user.orcid}/access_groups/{user.orcid}", headers = {"Authorization":f"Bearer {apikey}"})
        
            # for each project that the user is a USER on: 
            # 1. get the project object from the database
            # 2. add the project to the user object
            # 3. add an access group for the project, named by the project and add the user to the access group
            for pname in project_ids:
                props = session.exec(select(Project).where(Project.project_id == pname)).all()
                if len(props) != 1:
                    print(f"Projects found for {pname}: {props} --- skipping.  Please ensure a project exists with a unique name.")
                    continue
                else:
                    prop = props[-1]
                    
                    # add project to user
                    if prop not in user.projects:
                        user.projects.append(prop)
                    
                    # add user to access group
                    requests.request("post", url =  f"{api_url}/users/{user.orcid}/access_groups/{pname}", headers = {"Authorization":f"Bearer {apikey}"})
                    #ag = add_or_update(session, AccessGroup, AccessGroup.group_name==pname, group_name=pname)
                    #if not (user in ag.users):
                    #    ag.users.append(user)
        
            session.add(user)
            session.commit()
    return(user_info)


@app.get("/users/{orcid}/datasets", response_model = List[str])
def read_user_datasets(orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    """Retrieve all dataset IDs accessible to a specific user (Admin only).

    Returns a list of dataset unique identifiers that the specified user has access
    to through their access group memberships. This provides visibility into what
    data a user can view and work with.

    Args:
        orcid: ORCID identifier of the user to query
        admin_acct: Admin authorization dependency

    Returns:
        List[str]: List of dataset unique identifiers accessible to the user

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 404 if user with specified ORCID not found

    Access Control:
        - Admin access required
        - Returns datasets based on user's access group memberships

    Dataset Access Sources:
        - Datasets where user has explicit permissions via access groups
        - Datasets associated with user's projects
        - Datasets associated with user's instruments
        - Public datasets (if any)

    Response:
        - Returns empty list if user has no dataset access
        - Dataset IDs are unique identifiers, not internal database IDs
        - Includes all datasets with read or write permissions

    Use Cases:
        - Admin review of user permissions
        - Data access auditing
        - User onboarding verification
        - Troubleshooting access issues

    Note:
        This shows potential access based on group membership. Actual access
        may depend on dataset-specific ACL configurations.
    """
    with Session(engine) as session:
        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_list =session.exec(select(Dataset).select_from(join(DatasetACL, Dataset, DatasetACL.dataset_id == Dataset.id)).where(DatasetACL.accessgroup_id.in_(ag_ids))).all()
        
        return [ds.unique_id for ds in ds_list]


@app.get("/users/{orcid}/datasets/{dsid}", response_model = Dict)
def read_user_access_to_dataset(dsid: str, orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    """Check specific user permissions for a dataset (Admin only).

    Returns detailed permission information indicating whether the specified user
    has read and/or write access to the given dataset through their access group
    memberships.

    Args:
        dsid: Unique identifier of the dataset to check
        orcid: ORCID identifier of the user to check
        admin_acct: Admin authorization dependency

    Returns:
        Dict: Dictionary with 'read' and 'write' boolean permission flags

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 404 if user or dataset not found

    Access Control:
        - Admin access required
        - Checks permissions based on access group memberships

    Response Format:
        ```json
        {
            "read": true,
            "write": false
        }
        ```

    Permission Logic:
        - Checks all access groups the user belongs to
        - Returns true for permission type if ANY group grants that permission
        - Both permissions can be true for full access
        - Both false indicates no access to the dataset

    Use Cases:
        - Fine-grained permission verification
        - Access control debugging
        - User permission auditing
        - Dataset access troubleshooting

    Note:
        This provides more detailed permission info than the dataset list endpoint.
        Useful for verifying specific read/write permissions before operations.
    """
    with Session(engine) as session:
        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_acl = session.exec(select(DatasetACL).select_from(join(DatasetACL, Dataset, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))).all()
        perms = {"read": False, "write": False}
        for acc in ds_acl:
            if acc.read is True:
                perms['read'] = True
            if acc.write is True:
                perms['write'] = True
        return(perms)

@app.get("/users/{orcid}/projects", response_model = List[Project])
def read_projects_for_user(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    """Retrieve all projects associated with a specific user (Admin only).

    Returns a list of project objects where the specified user is associated as
    either a participant or contributor. This shows the user's involvement in
    research projects and their potential data access through project membership.

    Args:
        orcid: ORCID identifier of the user to query
        admin_acct: Admin authorization dependency

    Returns:
        List[Project]: List of project objects the user is associated with

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 404 if user with specified ORCID not found

    Access Control:
        - Admin access required
        - Shows projects based on UserProjectLink relationships

    Project Object Fields:
        - project_id: Unique project identifier
        - project_name: Human-readable project name
        - description: Project description
        - project_lead_email: Lead researcher contact
        - Additional project metadata

    Association Types:
        - User as project participant/contributor
        - Does NOT include projects where user is only the lead
        - Project membership determines dataset access permissions

    Response:
        - Returns empty list if user has no project associations
        - Includes all active project memberships
        - Projects may have multiple associated users

    Use Cases:
        - User project portfolio review
        - Research collaboration tracking
        - Project-based access control verification
        - User activity and involvement assessment

    Note:
        Project association provides access to project-related datasets
        and resources through automatic access group membership.
    """
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        user_id = user.id
        projects = session.exec(select(Project).select_from(join(Project, UserProjectLink, Project.project_id == UserProjectLink.project_id)).where(UserProjectLink.user_id == user_id)).all()
        return projects
    

@app.post("/projects", response_model = Project)
def create_project(auth_res: Annotated[tuple, Depends(authorize_user)], P: Project):
    """Create or update a research project with automatic access control setup.

    Creates a new project record or updates an existing one, automatically setting up
    access groups and associating the project lead. The project lead is automatically
    added to the project's access group for managing project resources.

    Args:
        auth_res: Authorization result containing access group name and IDs
        P: Project object containing project information

    Returns:
        Project: The created or updated project object

    Raises:
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if project data is invalid

    Access Control:
        - User must be authenticated
        - Creates access group named after project_id
        - Project lead automatically gets access group membership

    Automatic Setup:
        1. Creates/updates project record in database
        2. Creates access group named {project_id}
        3. Looks up project lead by email (lbl_email or email fields)
        4. Adds project lead to project access group
        5. Links project lead to project via UserProjectLink

    Project Lead Association:
        - Uses project_lead_email to find existing user account
        - Searches both lbl_email and email fields
        - Requires user account to exist before project creation
        - Automatically grants project lead access to project resources

    Access Group Benefits:
        - Controls access to project-related datasets
        - Manages permissions for project collaborators
        - Enables project-based data organization

    Note:
        Project participants are added separately via user management endpoints.
        Project lead must have an existing user account in the system.
    """
    with Session(engine) as session:
        prop = add_or_update(session, Project,  Project.id == P.id, **dict(P))
        ag = add_or_update(session, 
                      AccessGroup, 
                      AccessGroup.group_name==P.project_id, 
                      group_name=P.project_id)
        
        r1 = session.exec(select(User).where(User.lbl_email.ilike(f"%{P.project_lead_email}%"))).all()
        r2 = session.exec(select(User).where(User.email.ilike(f"%{P.project_lead_email}%"))).all()
        all_found_accounts = [x for x in r1 + r2 if x.orcid is not None]
        
        print(f"found accounts for user: {all_found_accounts}")

        if len(all_found_accounts) > 0:
            project_lead = all_found_accounts[-1]
            if not (project_lead in ag.users):
                ag.users.append(project_lead)

            if not (prop in project_lead.projects):
                project_lead.projects.append(prop)

        session.commit()
    return(P)


@app.get("/projects", response_model = List[Project])
def list_projects(admin_acct:  Annotated[str, Depends(is_admin)]):
    """Retrieve all projects in the system (Admin only).

    Returns a complete list of all research projects registered in the system,
    including project metadata, lead information, and organizational details.
    Useful for administrative oversight and project management.

    Args:
        admin_acct: Admin authorization dependency

    Returns:
        List[Project]: List of all project objects in the system

    Raises:
        HTTPException: 403 if user is not admin

    Access Control:
        - Admin access required
        - Regular users cannot list all projects

    Project Object Fields:
        - project_id: Unique project identifier
        - project_name: Human-readable project name
        - description: Project description and objectives
        - project_lead_email: Lead researcher contact information
        - start_date, end_date: Project timeline (if specified)
        - Additional project metadata and timestamps

    Response:
        - Returns empty list if no projects exist
        - Includes all projects regardless of status
        - Projects are returned in database order

    Use Cases:
        - Administrative project oversight
        - System-wide project reporting
        - Project portfolio management
        - Research activity tracking

    Note:
        For non-admin users, use GET /users/{orcid}/projects to see
        projects associated with specific users.
    """
    with Session(engine) as session:
        props = session.exec(select(Project)).all()
        return(props)


@app.get("/projects/{proj_id}", response_model = Union[None, Project])
def read_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Retrieve a specific project by its identifier.

    Returns the complete project record including metadata, lead information,
    and organizational details for the specified project ID. Returns None
    if the project doesn't exist.

    Args:
        proj_id: Unique identifier of the project to retrieve
        auth_res: Authorization result containing access group name and IDs

    Returns:
        Project | None: Project object if found, None if not found

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Should verify user has access to project (currently not enforced)
        - Returns project information if accessible

    Project Information:
        - Complete project metadata and description
        - Lead researcher contact information
        - Project timeline and organizational details
        - Associated funding and administrative information

    Response:
        - Returns None (not 404) if project doesn't exist
        - Includes all project fields and metadata
        - May include related information depending on implementation

    Use Cases:
        - Project detail viewing
        - Project information lookup
        - Research collaboration planning
        - Dataset context understanding

    Note:
        Access control should be enhanced to verify user has permission
        to view the specific project information.
    """
    with Session(engine) as session:
        prop = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        # should check user is in prop group
        return(prop)


@app.get("/projects/{proj_id}/users", response_model = List[User])
def read_users_on_project(proj_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    """Retrieve all users associated with a specific project (Admin only).

    Returns a list of user objects representing all researchers and collaborators
    who are participants in the specified project. This shows the project team
    membership and collaboration structure.

    Args:
        proj_id: Unique identifier of the project
        admin_acct: Admin authorization dependency

    Returns:
        List[User]: List of user objects associated with the project

    Raises:
        HTTPException: 403 if user is not admin
        HTTPException: 404 if project not found

    Access Control:
        - Admin access required
        - Shows project membership information

    User Association Types:
        - Project participants (via UserProjectLink)
        - Does NOT include project lead (handled separately)
        - Users added explicitly to project team

    User Object Fields:
        - orcid: User's ORCID identifier
        - first_name, last_name: Personal information
        - email, lbl_email: Contact information
        - Additional user metadata

    Response:
        - Returns empty list if no users are associated
        - Includes all active project participants
        - Users may be associated with multiple projects

    Use Cases:
        - Project team management
        - Collaboration tracking
        - Research team coordination
        - Access control verification

    Note:
        Project lead information is stored separately in the Project object.
        This endpoint shows participating team members only.
    """
    with Session(engine) as session:
        users = session.exec(select(User).select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id)).where(UserProjectLink.project_id == proj_id)).all()
        return users
        
@app.post("/samples/{parent_sample_id}/children/{child_sample_id}", response_model = SampleLink)
def link_parent_child(parent_sample_id, child_sample_id):
    """Create a parent-child relationship between two samples.

    Establishes a hierarchical relationship where one sample is derived from
    or created from another sample. This enables tracking of sample processing
    workflows, derivation chains, and experimental hierarchies.

    Args:
        parent_sample_id: Unique identifier of the parent sample
        child_sample_id: Unique identifier of the child sample

    Returns:
        SampleLink: The created sample relationship link

    Raises:
        HTTPException: 404 if either sample not found
        HTTPException: 422 if relationship data is invalid

    Sample Relationships:
        - Parent samples are source materials or starting points
        - Child samples are derived, processed, or subdivided samples
        - Multiple children can be derived from one parent
        - Sample processing chains can be tracked through links

    Link Object Fields:
        - parent_sample_id: Database ID of the parent sample
        - sample_id: Database ID of the child sample
        - Additional relationship metadata

    Use Cases:
        - Sample preparation workflow tracking
        - Material derivation documentation
        - Processing chain reconstruction
        - Sample provenance and lineage
        - Experimental hierarchy organization

    Examples:
        - Tissue sample → Prepared sections
        - Bulk material → Individual specimens
        - Raw sample → Processed/treated samples
        - Original → Replicate samples

    Note:
        Sample relationships help track experimental workflows
        and maintain sample provenance for research reproducibility.
    """

    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).first()
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).first()
        link = SampleLink(parent_sample_id=parent_sample.id, sample_id=child_sample.id)
        session.add(link)
        session.commit()
        session.refresh(link)
        
        return(link)

    
@app.post("/samples", response_model = Sample)
def create_sample(auth_res: Annotated[tuple, Depends(authorize_user)], S: Sample):
    """Create a new sample record in the system.

    Creates a new sample with metadata, ownership, and access control setup.
    Automatically assigns ownership to the creating user if not specified,
    and establishes the sample in the system for dataset association.

    Args:
        auth_res: Authorization result containing access group name and IDs
        S: Sample object containing sample information and metadata

    Returns:
        Sample: The created sample object with assigned ID

    Raises:
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if sample data is invalid

    Access Control:
        - User must be authenticated
        - Sample ownership automatically assigned if not specified

    Sample Fields:
        - unique_id: System-generated unique identifier
        - sample_name: Human-readable sample name
        - description: Sample description and characteristics
        - owner_orcid: ORCID of the sample owner
        - owner_user_id: Database ID of the owner
        - Additional metadata and experimental information

    Ownership Assignment:
        - Uses specified owner_user_id or owner_orcid if provided
        - Falls back to creating user's ORCID if none specified
        - Links sample to user account for access control

    Automatic Setup:
        1. Creates sample record with metadata
        2. Assigns ownership to user if not specified
        3. Creates IdType record for sample tracking
        4. Establishes sample in system for dataset association

    Use Cases:
        - Physical sample registration
        - Research material tracking
        - Experimental specimen documentation
        - Sample inventory management

    Note:
        Sample creation enables dataset-sample associations
        and supports experimental workflow tracking.
    """
    access_group_name, ag_ids = auth_res
   
    with Session(engine) as session:
        print("starting sample addition")
        sample = add_or_update(session,
                           Sample,
                           Sample.unique_id == S.unique_id,
                           **S.model_dump(exclude_unset = True, exclude = {"parents", "children"}))
        session.commit()
        session.refresh(sample)

        if sample.owner_user_id is None and sample.owner_orcid is None:
            creator = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).one_or_none()
            if is_orcid(access_group_name):
                sample.owner_orcid = access_group_name
                creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
                sample.owner_user_id = creator_user_id
                
        elif sample.owner_user_id is None:
            creator_user_id = session.exec(select(User.id).where(User.orcid == sample.owner_orcid)).one_or_none()
            sample.owner_user_id = creator_user_id
            
        else:
            pass
            
        session.add(sample)
        print('readded sample')
        add_or_update(session,
                      IdType,
                      where=(IdType.unique_id == sample.unique_id),
                      unique_id = sample.unique_id,
                      object_type = "sample")
        
        session.commit()
        print('final commit')
        session.refresh(sample)
        return(sample)


@app.post("/datasets/{dsid}/samples/{sample_id}", response_model = DatasetSampleLinkResponse)
def add_sample_to_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, sample_id: str):
    """Associate a sample with a dataset.

    Creates a link between an existing sample and dataset, establishing the
    relationship that the dataset contains data from or about the specified sample.
    This enables sample-based data organization and tracking.

    Args:
        auth_res: Authorization result containing access group name and IDs
        dsid: Unique identifier of the dataset
        sample_id: Unique identifier of the sample to associate

    Returns:
        DatasetSampleLinkResponse: Confirmation with dataset and sample details

    Raises:
        HTTPException: 404 if dataset or sample not found, or user lacks access
        HTTPException: 401/403 if user is not authorized
        HTTPException: 409 if association already exists

    Access Control:
        - User must have write access to the dataset
        - Sample must exist in the system

    Response Fields:
        - dataset_id: ID of the dataset
        - dataset_name: Name of the dataset
        - sample_id: ID of the sample
        - sample_name: Name of the sample

    Note:
        This creates a many-to-many relationship - samples can be associated
        with multiple datasets, and datasets can contain multiple samples.
    """
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    samp_cmd = select(Sample).where(Sample.unique_id == sample_id)

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()
        samp = session.exec(samp_cmd).one_or_none()

        if samp is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        
        newlink = DatasetSampleLink(sample_id = samp.id, dataset_id = ds.id)
        session.add(newlink)
        session.commit()

        info = DatasetSampleLinkResponse(dataset_id = ds.unique_id, dataset_name = ds.dataset_name, sample_id = samp.unique_id, sample_name = samp.sample_name)
    return info


@app.get("/samples", response_model = List[SampleWithDatasetsResponse])
def list_samples(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    """List samples accessible to the authenticated user with optional filtering.

    Returns samples owned by the requesting user or all samples if using admin
    access. Supports filtering by sample attributes through query parameters
    and includes associated dataset information for each sample.

    Args:
        auth_res: Authorization result containing access group name and IDs
        request: FastAPI Request object containing query parameters

    Returns:
        List[SampleWithDatasetsResponse]: List of samples with dataset associations

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - Admin access: Returns all samples in the system
        - User access: Returns only samples owned by the requesting user
        - ORCID-based filtering for regular users

    Query Parameters:
        - Any Sample model field for filtering (exact match)
        - Examples: sample_name, description, unique_id

    Response Format:
        Each sample includes:
        - Complete sample metadata
        - List of associated datasets
        - Ownership and creation information

    Access Patterns:
        - admin access_group: All samples visible
        - ORCID access_group: Only user's own samples
        - Other access_groups: Empty list (security)

    Examples:
        - GET /samples - User's samples (or all if admin)
        - GET /samples?sample_name=Test - Filter by name
        - GET /samples?description=blood - Filter by description

    Use Cases:
        - Personal sample inventory
        - Sample discovery and search
        - Administrative sample oversight
        - Research material tracking

    Note:
        Sample-dataset associations help track which data
        comes from which physical materials.
    """
    access_group_name, ag_ids = auth_res
    query_params = request.query_params
    print(query_params)
    
    params_dict = dict(query_params)
    print(params_dict)
    
    with Session(engine) as session:
        samp_cmd = select(Sample)
        
        # Admin API keys can see all samples
        if access_group_name == 'admin':
            # No owner filter for admin - they can see all samples
            pass
        # Filter by owner_orcid matching requester's ORCID (access_group_name when using ORCID-based API key)
        elif is_orcid(access_group_name):
            samp_cmd = samp_cmd.where(Sample.owner_orcid == access_group_name)
        else:
            # If not an ORCID-based request and not admin, return empty list for security
            return []
    
        for k, v in params_dict.items():
            if hasattr(Sample, k) and v is not None:
                samp_cmd = samp_cmd.where(getattr(Sample, k) == v)
            else:
               pass
            
        samp_cmd = samp_cmd.options(selectinload(Sample.datasets))
        samp_list = session.exec(samp_cmd).all()
        return samp_list


@app.get("/samples/{sample_id}", response_model = Union[None, SampleWithDatasetsResponse])
def read_sample(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Retrieve a specific sample by its unique identifier.

    Returns the complete sample record including metadata, ownership information,
    and all associated datasets for the specified sample ID. Includes dataset
    relationships to show what data has been generated from this sample.

    Args:
        sample_id: Unique identifier of the sample to retrieve
        auth_res: Authorization result containing access group name and IDs

    Returns:
        SampleWithDatasetsResponse | None: Sample with datasets if found, None if not found

    Raises:
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Sample access based on ownership and permissions

    Sample Information:
        - Complete sample metadata and description
        - Ownership and creation information
        - Physical characteristics and properties
        - Processing history and provenance

    Associated Datasets:
        - List of all datasets generated from this sample
        - Dataset metadata and access information
        - Experimental context and relationships

    Response:
        - Returns None (not 404) if sample doesn't exist
        - Includes complete sample and dataset information
        - Useful for understanding sample utilization

    Use Cases:
        - Sample detail viewing
        - Research material tracking
        - Dataset-sample relationship verification
        - Experimental workflow reconstruction

    Note:
        Sample-dataset associations provide traceability
        between physical materials and generated data.
    """
    with Session(engine) as session:
        query = select(Sample).options(selectinload(Sample.datasets)).where(Sample.unique_id == sample_id)
        samp = session.exec(query).first()
        return(samp)


@app.get("/datasets/{dsid}/samples", response_model = List[Sample])
def read_samples_in_dataset(dsid: str, auth_res:  Annotated[tuple, Depends(authorize_user)]):
    """Retrieve all samples associated with a dataset.

    Returns a list of sample objects that have been linked to the specified dataset,
    representing the physical samples that were measured, analyzed, or processed
    to generate the dataset.

    Args:
        dsid: Unique identifier of the dataset
        auth_res: Authorization result containing access group name and IDs

    Returns:
        List[Sample]: List of sample objects linked to the dataset

    Raises:
        HTTPException: 404 if dataset not found or user lacks access
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must have read access to the dataset
        - Public datasets are accessible to all authenticated users

    Sample Object Fields:
        - unique_id: Unique identifier for the sample
        - sample_name: Human-readable name
        - description: Sample description
        - owner_orcid: Sample owner identifier
        - Additional sample metadata

    Response:
        - Returns empty list if no samples are associated
        - Datasets can be linked to multiple samples
        - Samples can be associated with multiple datasets
        - Useful for tracking sample provenance and experimental relationships

    Note:
        This establishes the sample-to-dataset relationship, enabling
        sample-based organization and tracking of experimental data.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")
        
        samples = session.exec(select(Sample).select_from(join(Sample, DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)).where(DatasetSampleLink.dataset_id == ds.id)).all()
        return samples


@app.get("/samples/{sample_id}/datasets", response_model = List[Dataset])
def list_sample_datasets(sample_id: str, auth_res:  Annotated[tuple, Depends(authorize_user)]):
    """Retrieve all datasets associated with a specific sample.

    Returns a list of dataset objects that have been linked to the specified
    sample, showing all data that has been generated from or relates to the
    physical sample. Respects user access permissions for dataset visibility.

    Args:
        sample_id: Unique identifier of the sample
        auth_res: Authorization result containing access group name and IDs

    Returns:
        List[Dataset]: List of dataset objects associated with the sample

    Raises:
        HTTPException: 404 if sample not found
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Only returns datasets user has read access to
        - Includes public datasets regardless of permissions
        - Filtered by user's access group memberships

    Dataset Filtering:
        - Respects dataset access control lists (ACLs)
        - Public datasets visible to all authenticated users
        - Private datasets only visible to authorized users
        - Access determined by user's access group memberships

    Response:
        - Returns empty list if no accessible datasets exist
        - Includes complete dataset metadata
        - Shows sample-data relationships and experimental context

    Use Cases:
        - Sample utilization tracking
        - Data discovery from physical materials
        - Experimental workflow documentation
        - Research output verification
        - Sample-data traceability

    Examples:
        - Finding all microscopy data from a tissue sample
        - Discovering spectroscopy datasets from a material
        - Tracking all experiments performed on a specimen
        - Verifying data provenance for research validation

    Note:
        This establishes the critical link between physical samples
        and digital data for research reproducibility and traceability.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        datasets = session.exec(
            select(Dataset)
            .select_from(
                join(Dataset, DatasetSampleLink, DatasetSampleLink.dataset_id == Dataset.id)
                .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
            )
            .where(DatasetSampleLink.sample_id == 
                   select(Sample.id).where(Sample.unique_id == sample_id)
            )
            .filter(
                (DatasetACL.accessgroup_id.in_(ag_ids)) | 
                (Dataset.public == True)
            )
            .distinct()
        ).all()
        return datasets


@app.get("/samples/{parent_sample_id}/children", response_model = List[Sample])
def list_child_samples(parent_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    """Retrieve all child samples derived from a parent sample.

    Returns a list of samples that have been derived from, processed from,
    or created from the specified parent sample. This enables tracking of
    sample processing workflows and derivation hierarchies.

    Args:
        parent_sample_id: Unique identifier of the parent sample
        auth_res: Authorization result containing access group name and IDs

    Returns:
        List[Sample]: List of child sample objects derived from the parent

    Raises:
        HTTPException: 404 if parent sample not found
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Parent sample must exist and be accessible

    Parent-Child Relationships:
        - Child samples are derived from parent materials
        - Multiple children can come from one parent
        - Processing workflows create parent-child chains
        - Enables sample provenance tracking

    Response:
        - Returns empty list if no child samples exist
        - Includes complete metadata for each child sample
        - Shows sample processing and derivation history

    Use Cases:
        - Sample processing workflow tracking
        - Material derivation documentation
        - Processing chain visualization
        - Sample provenance verification
        - Experimental hierarchy navigation

    Examples:
        - Tissue sample → Multiple prepared sections
        - Bulk material → Individual test specimens
        - Original sample → Replicate preparations
        - Raw material → Processed variants

    Note:
        Sample hierarchies help track experimental workflows
        and maintain material provenance for research documentation.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Get parent sample
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).one_or_none()
        
        if parent_sample is None:
            raise HTTPException(status_code=404, detail="Parent sample not found")
        
        # Get child samples
        child_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.sample_id == Sample.id))
            .where(SampleLink.parent_sample_id == parent_sample.id)
        ).all()
        return child_samples


@app.get("/samples/{sample_id}/metadata/{sample_type}")
def get_sample_metadata(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    sample_id: str,
    sample_type: str
):
    """Retrieve type-specific metadata for a sample.

    Returns specialized metadata associated with a sample based on the sample type.
    Different sample types (biological, materials, geological, etc.) have different
    metadata schemas and this endpoint provides access to type-specific information.

    Args:
        auth_res: Authorization result containing access group name and IDs
        sample_id: Unique identifier of the sample
        sample_type: Type of sample metadata schema to retrieve (e.g., 'spinbot_batch')

    Returns:
        JSON: Sample metadata object specific to the sample type

    Raises:
        HTTPException: 404 if sample or metadata not found
        HTTPException: 400 if sample_type is invalid
        HTTPException: 401/403 if user is not authorized

    Access Control:
        - User must be authenticated
        - Sample must exist and be accessible
        - Metadata access based on sample permissions

    Sample Types:
        Different sample types support different metadata schemas:
        - spinbot_batch: Automated sample preparation metadata
        - biological: Biological sample information
        - materials: Material composition and properties
        - geological: Geological and environmental data
        - chemical: Chemical composition and analysis
        - Custom types as defined by research requirements

    Metadata Structure:
        - Type-specific fields and properties
        - Standardized schemas for consistency
        - Flexible JSON structure for extensibility
        - Research domain-specific information

    Response Format:
        Returns JSON object with type-specific metadata fields
        appropriate for the sample type and research domain.

    Use Cases:
        - Specialized sample characterization
        - Type-specific analysis parameters
        - Research domain metadata requirements
        - Sample processing protocol information

    Note:
        Metadata schemas are extensible and can be customized
        for specific research domains and sample types.
    """
    access_group_name, ag_ids = auth_res
    
    # Validate sample_type
    if sample_type not in SAMPLE_TYPE_MAPPING:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid sample_type '{sample_type}'. Supported types: {list(SAMPLE_TYPE_MAPPING.keys())}"
        )
    
    with Session(engine) as session:
        # Verify sample exists
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        
        # Get the appropriate model class based on sample_type
        model_class_name = SAMPLE_TYPE_MAPPING[sample_type]
        
        if model_class_name == "SpinbotBatchMetadata":
            model_class = SpinbotBatchMetadata
        else:
            raise HTTPException(status_code=500, detail=f"Model class {model_class_name} not implemented")
        
        # Get metadata
        metadata_obj = session.exec(
            select(model_class).where(model_class.sample_id == sample_id)
        ).one_or_none()
        
        if metadata_obj is None:
            raise HTTPException(
                status_code=404, 
                detail=f"No {sample_type} metadata found for sample {sample_id}"
            )
        
        return metadata_obj


@app.post("/samples/{sample_id}/metadata/{sample_type}")
def create_sample_metadata(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    sample_id: str,
    sample_type: str,
    metadata: dict
):
    """Create or update type-specific metadata for a sample.

    Creates or updates specialized metadata for a sample based on the sample type.
    This enables storage of domain-specific information, experimental parameters,
    and characterization data appropriate for different types of research samples.

    Args:
        auth_res: Authorization result containing access group name and IDs
        sample_id: Unique identifier of the sample
        sample_type: Type of sample metadata schema to use (e.g., 'spinbot_batch')
        metadata: Dictionary containing type-specific metadata to store

    Returns:
        JSON: Created or updated sample metadata object

    Raises:
        HTTPException: 404 if sample not found
        HTTPException: 400 if sample_type is invalid
        HTTPException: 401/403 if user is not authorized
        HTTPException: 422 if metadata format is invalid

    Access Control:
        - User must be authenticated
        - Sample must exist and be accessible
        - User needs write permissions for the sample

    Metadata Creation:
        - Creates new metadata record if none exists
        - Updates existing metadata if already present
        - Validates metadata against type-specific schema
        - Preserves metadata history and versioning

    Sample Type Schemas:
        Each sample type supports specific metadata fields:
        - spinbot_batch: Automated sample preparation metadata
        - biological: Species, tissue type, collection info
        - materials: Composition, properties, treatments
        - geological: Location, formation, analysis
        - chemical: Formula, purity, synthesis method
        - Custom schemas for specialized research

    Metadata Format:
        - JSON dictionary with type-specific fields
        - Extensible schema for research flexibility
        - Standardized field names for consistency
        - Support for nested structures and arrays

    Use Cases:
        - Sample characterization documentation
        - Experimental parameter recording
        - Research protocol information
        - Quality control and validation data
        - Provenance and processing history

    Note:
        Metadata schemas can be extended to support new
        research domains and specialized sample types.
    """
    access_group_name, ag_ids = auth_res
    
    # Validate sample_type
    if sample_type not in SAMPLE_TYPE_MAPPING:
        raise HTTPException(
            status_code=400, 
            detail=f"Invalid sample_type '{sample_type}'. Supported types: {list(SAMPLE_TYPE_MAPPING.keys())}"
        )
    
    with Session(engine) as session:
        # Verify sample exists
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()
        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")
        
        # Get the appropriate model class based on sample_type
        model_class_name = SAMPLE_TYPE_MAPPING[sample_type]
        
        if model_class_name == "SpinbotBatchMetadata":
            model_class = SpinbotBatchMetadata
        else:
            raise HTTPException(status_code=500, detail=f"Model class {model_class_name} not implemented")
        
        # Create metadata object with sample_id and sample_type
        metadata_dict = {
            "sample_id": sample_id,
            "sample_type": sample_type,
            **metadata
        }
        
        # Add or update metadata
        metadata_obj = add_or_update(
            session,
            model_class,
            model_class.sample_id == sample_id,
            **metadata_dict
        )
        
        session.commit()
        session.refresh(metadata_obj)
        
        return metadata_obj
    