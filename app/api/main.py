import datetime
import json
import os
import urllib.parse
from pathlib import Path
from typing import Annotated, Dict, List, Optional, Union

import pytz
import requests
import yaml
from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.security import HTTPBearer
from jose import jwt
from sqlalchemy import func, join, text
from sqlalchemy.orm import joinedload, selectinload
from sqlalchemy.sql import and_, or_
from sqlmodel import Session, select

from app.general_utils import get_secret, get_tz_isoformat, is_orcid, checkhash, remove_file, run_rclone_command
from app.models.core_models import (
    AccessGroup,
    AssociatedFile,
    Dataset,
    DatasetACL,
    DatasetCollection,
    DatasetLink,
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
from app.models.misc import DataDriveLocation, GoogleDrives, IdType
from app.models.response_schemas import AccountInfo, DatasetSampleLinkResponse, SampleWithDatasetsResponse
from app.models.tasks import GoogleDriveTransferRequest, IngestionRequest, SciCatUpdateRequest
from app.utils.constants import SAMPLE_TYPE_MAPPING
from app.utils.rmq_utils import send_pika_message
from app.utils.sql_utils import add_or_update, only_update, setup_engine, table_obj_to_dict

# ====== Database config
connect_args = {"check_same_thread": False}

home_dir = os.getenv("HOME")
db_config_file = f"{home_dir}/{os.getenv('DB_CONF_FILE')}"

engine = setup_engine(db_config_file)

api_config_file = os.getenv("API_CONF_FILE")
with open(f"{home_dir}/{api_config_file}") as f:
    apiconf = yaml.safe_load(f)

hostname = apiconf["hostname"]
api_path = apiconf["api_path"]
api_url = f"https://{hostname}/{api_path}"

# ======================= APP
app = FastAPI(root_path=f"/{api_path}")
security = HTTPBearer()
orcid_client_id = apiconf["orcid_client_id"]
orcid_client_secret = get_secret("CLIENT_SECRET", "orcid_client_secret/versions/2")
redirect_uri = urllib.parse.quote_plus(f"{api_url}/auth")

# ======================= AUTH AND ACCOUNT SET UP ENDPOINTS
from app.utils.auth_utils import authorize_user, is_admin

@app.get("/login")
def orcid_login() -> RedirectResponse:
    """
    Redirect the user to the ORCID login page for authentication.
    
    Returns:
        RedirectResponse to the ORCID login page
    """
    orcid_auth_url = f"https://orcid.org/oauth/authorize?response_type=code&redirect_uri={redirect_uri}&client_id={orcid_client_id}&scope=openid&nonce=whatever"
    response = RedirectResponse(orcid_auth_url)
    return response


@app.get("/auth")
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

    token = code_for_token_exchange(code, orcid_client_id, orcid_client_secret, f"{api_url}/auth")
    user_from_orcid = requests.get(
        url="https://orcid.org/oauth/userinfo",
        headers={"Authorization": f"Bearer {token}"}
    )
    try:
        user = user_from_orcid.json()
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


@app.get("/user_apikey")
def get_apikey(request: Request):
    """
    Get the user's API key based on their authentication token.
    
    Args:
        request: Request object containing the HTTP request
        
    Returns:
        API key if the user is authenticated, redirects to login page otherwise
    """
    from app.utils.auth_utils import decode_token, find_or_add_api_access
    user_info_jwt = request.cookies.get("crucible_user_token")
    
    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie("after_login_return_to_page", value=f"/{api_path}/user_apikey", httponly=True, secure=True)
        return response

    user_info = decode_token(user_info_jwt)
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

    return f"Your Crucible APIkey is: {crucible_token}"


# @app.post("/service_account/"):
# def create_service(service_name: str, read:bool = True, write:bool = False, is_admin: Annotated[str, Depends(is_admin)]):
#     from app.utils.auth_utils import decode_token, find_or_add_api_access
#     crucible_token = find_or_add_api_access(service_name, engine, regenerate=False)
#     with Session(engine) as session:
#         # confirm access group exists:
#         add_or_update(
#             session,
#             AccessGroup,
#             AccessGroup.group_name == service_name,
#             group_name=service_name
#         )
#     return {service_name, crucible_token}

@app.get("/regenerate_apikey")
def regenerate_apikey(request: Request):
    """
    Regenerate the user's API key based on their authentication token.
    
    Args:
        request: Request object containing the HTTP request
        
    Returns:
        New API key if the user is authenticated, redirects to login page otherwise
    """
    from app.utils.auth_utils import decode_token, find_or_add_api_access
    user_info_jwt = request.cookies.get("crucible_user_token")
    
    if user_info_jwt is None:
        response = RedirectResponse(f"/{api_path}/login")
        response.set_cookie("after_login_return_to_page", value=f"/{api_path}/regenerate_apikey", httponly=True, secure=True)
        return response

    user_info = decode_token(user_info_jwt)
    orcid = user_info["orcid"]
    crucible_token = find_or_add_api_access(orcid, engine, regenerate=True)

    return f"Your New Crucible APIkey is: {crucible_token}"



######################## 

@app.get("/account", response_model=AccountInfo)
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


@app.post("/datasets/{dsid}/carrier_segmentation")
def request_carrier_segmentation(dsid):
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")
    msg = {
        "dsid": dsid,
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="carrier-segmentation",
        user="admin",
        rmq_pw=rmq_pw
    )
    return
    

@app.post("/datasets", response_model=Dataset)
async def create_dataset(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dataset: Dataset
):
    print(f"received {Dataset}")
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds = add_or_update(
            session,
            Dataset,
            Dataset.unique_id == dataset.unique_id,
            **dataset.model_dump(exclude_unset=True)
        )
        session.commit()
        session.refresh(ds)
        
        print(f'added dataset {ds}')
        # set appropriate access controls
        admin = session.exec(select(AccessGroup).where(AccessGroup.group_name == "admin")).one_or_none()
        admin_acl = sql_create_ds_acl(admin, ds, read = True, write = True, session=session)

        ro = session.exec(select(AccessGroup).where(AccessGroup.group_name == "read_only")).one_or_none()
        ro_acl = sql_create_ds_acl(ro, ds, read = True, write = False, session=session)
        print(f"add ro access to {ds.unique_id}")

        if dataset.owner_user_id is not None:
            print(f'adding owner orcid access to {ds.unique_id}')
            user_orcid = session.exec(select(User.orcid).where(User.id == dataset.owner_user_id)).one_or_none()
            user_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user_orcid)).one_or_none()
            user_acl = sql_create_ds_acl(user_ag, ds, read = True, write = True, session=session)
            print(f'added owner orcid access to {ds.unique_id}')

        else:
            # use the apikey associated accessgroup as owner
            creator = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).one_or_none()
            print(f'{access_group_name=} for {ds.unique_id}')
            print(f'{creator=} for {ds.unique_id}')
            creator_acl = sql_create_ds_acl(creator, ds, read = True, write = True, session=session)
            print(f'add creator access to {ds.unique_id}')

            if is_orcid(access_group_name):
                ds.owner_orcid = access_group_name
                creator_user_id = session.exec(select(User.id).where(User.orcid == dataset.owner_orcid)).one_or_none()
                ds.owner_user_id = creator_user_id

        if dataset.project_id is not None:
            print(f'adding proposal access to {ds.unique_id}')
            prop_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == dataset.project_id)).one_or_none()
            prop_acl = sql_create_ds_acl(prop_ag, ds, read = True, write = True, session=session)
            print(f'added proposal access to {ds.unique_id}')

        if dataset.instrument_id is not None:
            print(f'adding instrument group access to {ds.unique_id}')
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
            print(f'added instrument group access to {ds.unique_id}')

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


@app.patch("/datasets/{dsid}", response_model = Dataset)
def update_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, updated_dataset: Dataset):
    '''
    Updates a dataset in SQL and returns the Updated Dataset object. 
    Sends a message to RabbitMQ to alert RMQ that updates have been made.
    '''
    access_group_name, ag_ids = auth_res
    #newds = table_obj_to_dict(updated_dataset)
    ds_cmd = select(Dataset).select_from(
                                join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(
                                    Dataset.unique_id == dsid).filter(
                                        DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if not ds_list:
        raise HTTPException(status_code=404, detail="Dataset not found")
    

    with Session(engine) as session:
        stored_ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if stored_ds:   
            stored_ds_dict = dict(stored_ds)
        print(f"{stored_ds_dict=}")

        update_attr = updated_dataset.model_dump(exclude_unset=True)
        print(f"{update_attr=}")
        filtered_updates = {k: v for k, v in update_attr.items()}

        
        upds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **filtered_updates)


        if upds.owner_user_id is not None:
            user_orcid = session.exec(select(User.orcid).where(User.id == upds.owner_user_id)).one_or_none()
            user_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user_orcid)).one_or_none()
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
   
 
def generate_kw_query(session, base_query, keyword_filter):

    keywords = session.exec(
                select(Keyword).where(Keyword.keyword.ilike(f"%{keyword_filter}%"))
            ).all()
    
    if not keywords:
        return []
    
    keyword_ids = [keyword.id for keyword in keywords]
    query = (base_query.select_from(
                    join(
                        DatasetKeywordLink,
                        DatasetKeywordLink.dataset_id == Dataset.id
                    )
                ).filter(DatasetKeywordLink.keyword_id.in_(keyword_ids))
            )
    return(query)



@app.get("/datasets")
def list_datasets(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request, include_metadata: bool = False, limit: Optional[int] = None):
    '''
    Returns a list of all dataset IDs the requesting user is authorized to view.
    If no dataset IDs are authorized for the user, an empty list will be returned.

    Args:
        auth_res: Authorization result containing access group info
        request: Request object containing query parameters
        limit: Optional maximum number of datasets to return
    '''
    access_group_name, ag_ids = auth_res
    params_dict = dict(request.query_params)
    keyword_filter = params_dict.pop('keyword', None)
    print(f'{include_metadata=}')
    with Session(engine) as session:
        # Build authorization filter (user's access groups or public datasets)
        auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)

        # if include_metadata - join on scientific metadata
        if include_metadata:
            query = (
                    select(Dataset)
                    .options(selectinload(Dataset.scientific_metadata))
                    .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                    .filter(auth_filter)
                )

        else:
            query = (select(Dataset)
                        .select_from(
                            join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                        ).filter(auth_filter)
                    )

        # join and filter on keywords
        if keyword_filter:
            query = generate_kw_query(session, query, keyword_filter)

        # Apply additional filters for Dataset attributes
        for key, value in params_dict.items():
            if hasattr(Dataset, key):
                query = query.where(getattr(Dataset, key) == value)

        # Apply limit if specified
        if limit is not None:
            query = query.limit(limit)

        results = session.exec(query.distinct(Dataset.id)).all()
        if include_metadata:
            return [{**dataset.model_dump(),
                    "scientific_metadata": dataset.scientific_metadata.model_dump() if dataset.scientific_metadata else None
                    }
                    for dataset in results]
        else:
            return results
    
    
@app.get("/datasets/{dsid}", response_model = Union[Dataset, None])
def read_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    '''
    Returns a dataset found with the provided unique ID
    if one is exists and the requesting user is
    authorized to view the dataset. Otherwise, returns None
    '''
    access_group_name, ag_ids = auth_res
    print(f"{ag_ids=}")
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        return ds


# WHAT ELSE?
# CAN UPDATE SAMPLE TO ONE SQL QUERY? TIME THIS

@app.post("/datasets/{parent_dataset_id}/children/{child_dataset_id}", response_model = DatasetLink)
def link_parent_child(parent_dataset_id, child_dataset_id, auth_res: Annotated[tuple, Depends(authorize_user)]):
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(
                                        join(Dataset,
                                                DatasetACL,
                                                DatasetACL.dataset_id == Dataset.id, isouter=True)
                                        ).filter(
                                                (DatasetACL.accessgroup_id.in_(ag_ids)) |
                                                (Dataset.public == True)
                                        )
    
    with Session(engine) as session:
        parent = session.exec(ds_cmd.where(Dataset.unique_id == parent_dataset_id)).first()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent dataset not found")
        
        child = session.exec(ds_cmd.where(Dataset.unique_id == child_dataset_id)).first()
       # print(f'{child=}')
        if not child:
            raise HTTPException(status_code=404, detail="Child dataset not found")
        
        link = add_or_update(session, 
                     DatasetLink,
                     where = (and_(DatasetLink.parent_dataset_id == parent.id, 
                                   DatasetLink.child_dataset_id == child.id)), 
                     parent_dataset_id = parent.id, 
                     child_dataset_id = child.id)
        
        session.commit()
        session.refresh(link)
        
        return(link)

@app.get("/datasets/{child_dataset_id}/parents", response_model = List[Dataset])
def list_parent_datasets(child_dataset_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of parent datasets for a given dataset
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Get parent sample
        ds_cmd = select(Dataset).select_from(
                                            join(Dataset,
                                                    DatasetACL,
                                                    DatasetACL.dataset_id == Dataset.id, isouter=True)
                                            ).filter(
                                                    (DatasetACL.accessgroup_id.in_(ag_ids)) |
                                                    (Dataset.public == True)
                                            )
        child = session.exec(ds_cmd.where(Dataset.unique_id == child_dataset_id)).one_or_none()
        
        if child is None:
            raise HTTPException(status_code=404, detail="Child dataset not found")
        
        # TODO: JOIN ON ACL BUT PERMISSIONS SHOULD PROBABLY INHERIT..
        # Get child samples
        parents = session.exec(
            select(Dataset)
            .select_from(join(Dataset, DatasetLink, DatasetLink.parent_dataset_id == Dataset.id))
            .where(DatasetLink.child_dataset_id == child.id)
        ).all()
        return parents
    

@app.get("/datasets/{parent_dataset_id}/children", response_model = List[Dataset])
def list_child_datasets(parent_dataset_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of child datasets for a given parent dataset
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Get parent sample
        ds_cmd = select(Dataset).select_from(
                                            join(Dataset,
                                                    DatasetACL,
                                                    DatasetACL.dataset_id == Dataset.id, isouter=True)
                                            ).filter(
                                                    (DatasetACL.accessgroup_id.in_(ag_ids)) |
                                                    (Dataset.public == True)
                                            )
        parent = session.exec(ds_cmd.where(Dataset.unique_id == parent_dataset_id)).one_or_none()
        
        if parent is None:
            raise HTTPException(status_code=404, detail="Parent sample not found")
        
        # TODO: JOIN ON ACL BUT PERMISSIONS SHOULD PROBABLY INHERIT..
        # Get child samples
        children = session.exec(
            select(Dataset)
            .select_from(join(Dataset, DatasetLink, DatasetLink.child_dataset_id == Dataset.id))
            .where(DatasetLink.parent_dataset_id == parent.id)
        ).all()
        return children



@app.delete("/datasets/{dsid}", response_model = Dataset)
def delete_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid = str):
    access_group_name, ag_ids = auth_res
    # TODO: DEFINE THIS
    pass


@app.get('/datasets/{dsid}/download_links')
def generate_signed_url_download_link(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, bucket: str='mf-storage-prod'):
    from google.cloud import storage
    from datetime import timedelta

    # TODO - instead of all this credential passing - should set up service specific service account and give correct permissions
    from google.oauth2 import service_account
    sa_creds = get_secret("GCS_SA", "service-account-cred/versions/6")
    service_account_info = json.loads(sa_creds)
    credentials = service_account.Credentials.from_service_account_info(service_account_info)

    storage_client = storage.Client(credentials = credentials)
    bucket = storage_client.bucket(bucket)
    ds_blobs = bucket.list_blobs(prefix = f'{dsid}/')
    urls = {}
    for blob in ds_blobs:
        urls[blob.name] = blob.generate_signed_url(
        version="v4",
        expiration=timedelta(hours=1),
        method="GET")

    return urls


@app.post("/datasets/{dsid}/upload", response_model = AssociatedFile)
async def create_dataset_from_file_upload(files: Annotated[List[UploadFile], File(description="List of files")],
                                          auth_res:  Annotated[tuple, Depends(authorize_user)],
                                          dsid: str):
    '''
    Upload files to cloud bucket and add to associated files list for dataset. 
    '''
    
    access_group_name, ag_ids = auth_res
    # upload the file
    for file in files:
        dest_dir = f"/mnt/gcs/api-uploads/{os.path.dirname(file.filename)}"
            
        Path(dest_dir).mkdir(parents=True, exist_ok=True)
        save_file_path = f"/mnt/gcs/api-uploads/{file.filename}"
        with open(save_file_path, "wb") as f:
            f.write(await file.read())

    # create associated files record
    af = {"filename": os.path.join("api-uploads", file.filename), 
          "size": os.path.getsize(save_file_path),
          "sha256_hash": checkhash(save_file_path)}
    
    # add associated file
    added_af = add_associated_file(auth_res, dsid, AssociatedFile(**af))
    return(added_af)


@app.post("/datasets/{dsid}/ingest", response_model = IngestionRequest)
def request_dataset_ingestion(auth_res: Annotated[tuple, Depends(authorize_user)],
                              dsid: str,
                              file_to_upload: Optional[str]=None,
                              ingestion_class: Optional[str]=None):
    
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
    msg = {
        "filename": file_to_upload,
        "reqid": ingestion_request.id,
        "dsid": dsid,
        "ingestion_class": ingestion_class
    }

    send_pika_message(
        message=json.dumps(msg),
        exchange="",
        routing_key="ingest-newapi",
        user="admin",
        rmq_pw=rmq_pw
    )
    
    return ingestion_request


@app.get("/datasets/{dsid}/scicat_update/{reqid}", response_model = SciCatUpdateRequest)
def get_scicat_update_request(auth_res: Annotated[tuple, Depends(authorize_user)], reqid: int, dsid: str):

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
    """
    Get a specific ingestion request by dataset ID and request ID.
    
    Args:
        auth_res: Authorization result containing access group info
        reqid: Ingestion request ID
        dsid: Dataset ID
        
    Returns:
        IngestionRequest object if found and user has access
        
    Raises:
        HTTPException: If request not found or access denied
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
    if ing_req.id:
        print(f"{reqid=}")
        print(f"{ing_req.id=}")
        assert reqid == ing_req.id
        
    with Session(engine) as session:
        updated_req = only_update(session,
                                  IngestionRequest,
                                  (IngestionRequest.id == reqid) & (IngestionRequest.dataset_id == dsid), 
                                  **ing_req.model_dump(exclude_unset=True))
        if updated_req is None:
            errmsg = "No ingestion request found for the \
                      provided dataset and request ID combination"
            
            raise HTTPException(status_code=400,
                                detail=errmsg)
        
        session.commit()
        session.refresh(updated_req)
        
    return(updated_req)


@app.post("/datasets/{dsid}/google_drive_transfer", response_model=GoogleDriveTransferRequest)
def request_dataset_transfer_to_drive(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    """
    For now keep simple - Input is a dataset.
    Drive info is derived from Project and Owner.
    Location within drive is hardcoded.
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        creation_time = get_tz_isoformat()

        ds_cmd = select(Dataset).select_from(
            join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)
        ).where(Dataset.unique_id == dsid).filter(
            DatasetACL.accessgroup_id.in_(ag_ids)
        ).distinct()

        ds_list = session.exec(ds_cmd).all()

        if not ds_list:
            raise HTTPException(status_code=404, detail="Dataset not found")

        transfer_request = GoogleDriveTransferRequest(dataset_id = dsid, time_created=creation_time, status = "requested")
        session.add(transfer_request)
        session.commit()
        session.refresh(transfer_request)

        message = json.dumps({
            "dsid": dsid,
            "source_bucket": "mf-storage-prod",
            "reqid": transfer_request.id
        })
        rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")

        send_pika_message(
            message,
            exchange="",
            routing_key="transfer-newapi",
            user="admin",
            rmq_pw=rmq_pw
        )
        return transfer_request


@app.patch("/datasets/{dsid}/google_drive_transfer/{reqid}", response_model = GoogleDriveTransferRequest)
def patch_transfer_request(is_admin: Annotated[str, Depends(is_admin)], reqid: int, dsid: str, xfer_req: GoogleDriveTransferRequest):
    if xfer_req.id:
        print(f"{reqid=}")
        print(f"{xfer_req.id=}")
        assert reqid == xfer_req.id
    with Session(engine) as session:
        updated_req = only_update(session, GoogleDriveTransferRequest, (GoogleDriveTransferRequest.id == reqid) & (GoogleDriveTransferRequest.dataset_id == dsid), **xfer_req.model_dump(exclude_unset=True))
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.post("/datasets/{dsid}/scicat_update", response_model=SciCatUpdateRequest)
def request_update_scicat_dataset(
    auth_res: Annotated[tuple, Depends(authorize_user)],
    dsid: str,
    overwrite_data: bool = False
):
    creation_time = get_tz_isoformat()
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(
        join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)
    ).where(Dataset.unique_id == dsid).filter(
        DatasetACL.accessgroup_id.in_(ag_ids)
    ).distinct()

    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if not ds_list:
        raise HTTPException(status_code=404, detail="Dataset not found")

    # Should add this to database table but it is okay for now
    scicat_request = SciCatUpdateRequest(dataset_id = dsid, time_created = creation_time, status = "requested")
    session.add(scicat_request)
    session.commit()
    session.refresh(scicat_request)

    message = json.dumps({
        "dsid": dsid,
        "source_bucket": "mf-storage-prod",
        "reqid": scicat_request.id,
        "overwrite_data": overwrite_data
    })
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "rabbitmq_default_pw/versions/1")

    send_pika_message(
        message,
        exchange="",
        routing_key="scicat-newapi",
        user="admin",
        rmq_pw=rmq_pw
    )
    return scicat_request


@app.patch("/datasets/{dsid}/scicat_update/{reqid}", response_model = SciCatUpdateRequest)
def patch_scicat_request(is_admin: Annotated[str, Depends(is_admin)], reqid: int, dsid: str, sc_req: SciCatUpdateRequest):
    if sc_req.id:
        assert reqid == sc_req.id
    with Session(engine) as session:
        updated_req = only_update(session, SciCatUpdateRequest, (SciCatUpdateRequest.id == reqid) & (SciCatUpdateRequest.dataset_id == dsid), **sc_req.model_dump(exclude_unset=True))
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.get("/datasets/{dsid}/scientific_metadata")
def read_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_md = session.exec(select(ScientificMetadata).where(ScientificMetadata.dataset_unique_id == ds.unique_id)).one_or_none()
        return ds_md


@app.post("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def add_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, scientific_md: dict):
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
    '''
    Returns a list of thumbnail objects associated with the dataset ID provided
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_thumbnails = session.exec(select(Thumbnail).where(Thumbnail.dataset_id == ds.id)).all()
        return ds_thumbnails


@app.post("/datasets/{dsid}/thumbnails", response_model = Thumbnail)
def add_thumbnail(auth_res: Annotated[tuple, Depends(authorize_user)],
                  dsid: str,
                  thumbnail: Thumbnail):
    '''
    Adds a thumbnail to a dataset. If the thumbnail exists, it will simply add the existing thumbnail to the dataset object.
    Otherwise, the thumbnail will be created and then added to the dataset.

    Raises:
        HTTPException: If required thumbnail fields are missing or dataset not found
    '''
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if not thumbnail.thumbnail_name or not thumbnail.thumbnail_name.strip():
        missing_fields.append("thumbnail_name")
    if not thumbnail.thumbnail_b64str or not thumbnail.thumbnail_b64str.strip():
        missing_fields.append("thumbnail_b64str")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required thumbnail information: {', '.join(missing_fields)}. Please provide all required fields: thumbnail_name and thumbnail_b64str."
        )

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
    '''
    Returns a list of the files associated with the dataset ID provided.
    '''
    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_files = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)).all()
        return ds_files


@app.post("/datasets/{dsid}/associated_files", response_model = AssociatedFile)
def add_associated_file(auth_res: Annotated[tuple, Depends(authorize_user)],
                        dsid: str,
                        associated_file: AssociatedFile):
    '''
    Adds an associated file to a dataset.
    Creates the AF object if it doesn't already exist.

    Raises:
        HTTPException: If required associated file fields are missing or dataset not found
    '''
    print("accepted args")
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if not associated_file.filename or not associated_file.filename.strip():
        missing_fields.append("filename")
    if associated_file.size is None:
        missing_fields.append("size")
    if not associated_file.sha256_hash or not associated_file.sha256_hash.strip():
        missing_fields.append("sha256_hash")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required associated file information: {', '.join(missing_fields)}. Please provide all required fields: filename, size, and sha256_hash."
        )

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

    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=403, detail="Dataset not found or access denied")

        ds_keywords = session.exec(select(Keyword).select_from(join(Keyword, DatasetKeywordLink, DatasetKeywordLink.keyword_id == Keyword.id)).where(DatasetKeywordLink.dataset_id == ds.id)).all()
        return ds_keywords


@app.post("/datasets/{dsid}/keywords", response_model = Keyword)
def add_keyword(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, keyword: str):
    '''
    Adds a keyword to a dataset. If the keyword exists, it will simply add the existing keyword to the dataset object. 
    Otherwise, the keyword will be created and then added to the dataset. 
    '''
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
    '''
    Returns a list of DataDriveLocation objects with one object for each location in google drive where the dataset is currently located.
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Verify dataset exists and user has access
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")

        ds_locs = session.exec(select(DataDriveLocation).where(and_(DataDriveLocation.dataset_id == dsid, DataDriveLocation.status == 'current'))).all()
    return(ds_locs)


@app.post("/datasets/{dsid}/drive_location", response_model = DataDriveLocation)
def create_dataset_drive_location(auth_res:  Annotated[tuple, Depends(authorize_user)], dsid: str, drive_info: DataDriveLocation):
    """
    Add a Google Drive location record to the database.

    Args:
        ag_ids: List of access group IDs the user belongs to
        dsid: Dataset ID
        D: Google Drive information to be added as DataDriveLocation object

    Returns:
        Added DataDriveLocation object

    Raises:
        HTTPException: If required drive location fields are missing or dataset not found
    """
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if not drive_info.drive_id or not drive_info.drive_id.strip():
        missing_fields.append("drive_id")
    if not drive_info.drive_name or not drive_info.drive_name.strip():
        missing_fields.append("drive_name")
    if not drive_info.folder_path_in_drive or not drive_info.folder_path_in_drive.strip():
        missing_fields.append("folder_path_in_drive")
    if not drive_info.folder_id or not drive_info.folder_id.strip():
        missing_fields.append("folder_id")
    if not drive_info.url or not drive_info.url.strip():
        missing_fields.append("url")
    if not drive_info.status or not drive_info.status.strip():
        missing_fields.append("status")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required drive location information: {', '.join(missing_fields)}. Please provide all required fields: drive_id, drive_name, folder_path_in_drive, folder_id, url, and status."
        )

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
    with Session(engine) as session:
        # Verify dataset exists
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        dscmd = select(AccessGroup).select_from(join(AccessGroup, join(DatasetACL,
                                                                     Dataset,
                                                                     DatasetACL.dataset_id == Dataset.id),
                                                    AccessGroup.id == DatasetACL.accessgroup_id))
        dscmd = dscmd.where(Dataset.unique_id == dsid)
        ds_acl = session.exec(dscmd).all()

        return ds_acl


def sql_create_ds_acl(ag, ds, read, write, session):
    acl = session.exec(select(DatasetACL).where(DatasetACL.accessgroup == ag).where(DatasetACL.dataset == ds)).one_or_none()
    print(f"{acl=} for {ds.unique_id}")
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
    with Session(engine) as session:
        print(f"{dsid=}")
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()

        acl = sql_create_ds_acl(ag, ds, read, write, session)
    return(acl)


@app.post("/dataset_collections", response_model = DatasetCollection)
def create_dataset_collection(auth_res:  Annotated[tuple, Depends(authorize_user)], dc: DatasetCollection):
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        session.add(dc)
        session.commit()
        session.refresh(dc)
    return(dc)


@app.get("/dataset_collections/{dcid}", response_model = Union[None, DatasetCollection])
def read_dataset_collection(admin_acct: Annotated[str, Depends(is_admin)], dcid: str):
    with Session(engine) as session:
        dc = session.exec(select(DatasetCollection).where(DatasetCollection.unique_id == dcid)).one_or_none()
    return(dc)


@app.get("/dataset_collections", response_model = List[DatasetCollection])
def list_dataset_collections(admin_acct: Annotated[str, Depends(is_admin)], request: Request):
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
    """
    Create a new access group in the database.

    Args:
        admin_acct: Admin account identifier
        ag: AccessGroup object to be added

    Returns:
        Added AccessGroup object

    Raises:
        HTTPException: If required access group fields are missing
    """
    # Validate required fields
    missing_fields = []
    if not ag.group_name or not ag.group_name.strip():
        missing_fields.append("group_name")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required access group information: {', '.join(missing_fields)}. Please provide all required fields: group_name."
        )

    with Session(engine) as session:
        session.add(ag)
        session.commit()
    return(ag)


@app.get("/access_groups/{group_name}", response_model = Union[None, AccessGroup])
def read_access_group(admin_acct: Annotated[str, Depends(is_admin)], group_name: str):
    with Session(engine) as session:
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
        return(ag)


@app.post("/users/{orcid}/access_groups/{group_name}", response_model = AccessGroup)
def add_user_to_access_group(admin_acct: Annotated[str, Depends(is_admin)], orcid: str, group_name: str):
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
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Verify user exists
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        ags = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
        return([ag.group_name for ag in ags])



# =====================

@app.get("/keywords", response_model = List[Keyword])
def list_all_keywords():
    '''
    Returns a list of all keywords in the database.
    Each keyword includes the num_datasets field showing how many datasets use that keyword.
    '''
    with Session(engine) as session:
        keywords = session.exec(select(Keyword)).all()
    return keywords


@app.get("/keywords/{keyword}", response_model = List[Keyword])
def read_keyword(keyword: str, permissive: bool = True):
    '''
    Returns a list of all keyword entries that match the provided keyword.
    If permissive is set to False, then all returned entries will be an exact match to the provided string.
    Otherwise, keywords will be searched as case insensitive.  The default configuration has permissive set to True.
    '''
    if permissive:
        cmd = select(Keyword).where(Keyword.keyword.ilike(f"{keyword.lower()}"))
    else:
        cmd = select(Keyword).where(Keyword.keyword == keyword)

    with Session(engine) as session:
        K = session.exec(cmd).all()
    return(K)


@app.get("/instruments", response_model = List[Instrument])
def list_instrument(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    '''
    Returns a list of instruments filtered by any provided search terms that are attributes of the instrument Class. 
    If no instruments matching the search criteria are found, an empty list is returned.  
    Search parameters include these Instrument attributes (Note: not case sensitive, but otherwise must be exact match): 
        other_id: str
        other_id_source: str
        instrument_name: str
        manufacturer: str
        model: str
        owner: str
        location: str
        description: str
        instrument_type: str
    '''
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
    '''
    Returns an instrument with the provided ID if one exists. Otherwise returns None. 
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        inst = session.exec(select(Instrument).where(Instrument.unique_id == instrument_unique_id)).one_or_none()
        return(inst)


@app.post("/instruments", response_model = Instrument)
def create_instrument(auth_res:  Annotated[tuple, Depends(authorize_user)], instrument: Instrument):
    """
    Add a new instrument record to the database.

    Args:
        ag_ids: List of access group IDs the user belongs to; here only used to confirm user is in the database
        instrument: Instrument object to be added

    Returns:
        Added instrument object

    Raises:
        HTTPException: If required instrument fields are missing
    """
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if not instrument.instrument_name or not instrument.instrument_name.strip():
        missing_fields.append("instrument_name")
    if not instrument.owner or not instrument.owner.strip():
        missing_fields.append("owner")
    if not instrument.location or not instrument.location.strip():
        missing_fields.append("location")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required instrument information: {', '.join(missing_fields)}. Please provide all required fields: instrument_name, owner, and location."
        )

    instrument.instrument_name = instrument.instrument_name.lower()
    with Session(engine) as session:
        session.add(instrument)
        session.commit()
        session.refresh(instrument)
    return(instrument)


@app.get("/google_drives", response_model = List[GoogleDrives])
def list_google_drive_info(auth_res:  Annotated[tuple, Depends(authorize_user)], request: Request):
    """
    Returns a list of google drive information
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
    """
    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        drive_id: Google Drive Name to query
        
    Returns:
        Google Drive object if found, otherwise None
    """
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        drive = session.exec(select(GoogleDrives).where(GoogleDrives.drive_name == drive_name)).one_or_none()

    return(drive)


@app.post("/google_drives", response_model = GoogleDrives)
def create_google_drive_info(auth_res:  Annotated[tuple, Depends(authorize_user)], D: Dict):
    """
    Add or update a Google Drive record in the database.

    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        D: Google Drive information to be added or updated

    Returns:
        Added or updated Google Drive object

    Raises:
        HTTPException: If required Google Drive fields are missing
    """
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if "drive_id" not in D or not D["drive_id"] or not str(D["drive_id"]).strip():
        missing_fields.append("drive_id")
    if "drive_name" not in D or not D["drive_name"] or not str(D["drive_name"]).strip():
        missing_fields.append("drive_name")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required Google Drive information: {', '.join(missing_fields)}. Please provide all required fields: drive_id and drive_name."
        )

    with Session(engine) as session:
        try:
            dr = add_or_update(
                session,
                GoogleDrives,
                GoogleDrives.drive_id == D["drive_id"],
                **D
            )
            session.commit()
            session.refresh(dr)
        except Exception as err:
            print(err)

    return D

# =======================================
@app.get("/users", response_model = List[User])
def list_users(admin_acct:  Annotated[str, Depends(is_admin)], request: Request, permissive = True):

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
    '''
    Returns the User object associated with the provided ORCID if found. Otherwise returns None. 
    '''
    with Session(engine) as session:
        users = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        return users


@app.post("/users", response_model = User)
def create_user(admin_acct:  Annotated[str, Depends(is_admin)], user_info: User, project_ids: List[str] = Body(...)):
    """
    Add or update a user record in the database.
    Add the user to an Access Group with their ORCID
    Add the user to Access Groups for any project they are a USER on
         // DOES NOT ADD Users to access groups for the projects
            on which they are the project lead
    Create a link to the projects that the User is a USER on

    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        user_info: Instance of class User

    Returns:
        Added or updated user object

    Raises:
        HTTPException: If required user fields are missing
    """
    # Validate required fields
    missing_fields = []
    if not user_info.first_name or not user_info.first_name.strip():
        missing_fields.append("first_name")
    if not user_info.last_name or not user_info.last_name.strip():
        missing_fields.append("last_name")
    if not user_info.orcid or not user_info.orcid.strip():
        missing_fields.append("orcid")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required user information: {', '.join(missing_fields)}. Please provide all required fields: first_name, last_name, and orcid."
        )

    with Session(engine) as session:
        # add user to user table
        user_dict = table_obj_to_dict(user_info)
        user = add_or_update(session, User, User.orcid == user_info.orcid, **user_dict)
        session.commit()

        apikey = get_secret("ADMIN_APIKEY", "crucible_admin_apikey/versions/3")
        requests.post(
            f"{api_url}/users/{user.orcid}/access_groups/{user.orcid}",
            headers={"Authorization": f"Bearer {apikey}"}
        )

        # for each project that the user is a USER on:
        # 1. get the project object from the database
        # 2. add the project to the user object
        # 3. add an access group for the project, named by the project and add the user to the access group
        for pname in project_ids:
            props = session.exec(select(Project).where(Project.project_id == pname)).all()
            if len(props) != 1:
                print(f"Projects found for {pname}: {props} --- skipping.  Please ensure a project exists with a unique name.")
                continue

            prop = props[-1]

            # add project to user
            if prop not in user.projects:
                user.projects.append(prop)

            # add user to access group
            requests.post(
                f"{api_url}/users/{user.orcid}/access_groups/{pname}",
                headers={"Authorization": f"Bearer {apikey}"}
            )

        session.add(user)
        session.commit()
    return user_info


@app.get("/users/{orcid}/datasets", response_model = List[str])
def read_user_datasets(orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of all the datasets the user has access to based on the ORCID provided.
    '''
    with Session(engine) as session:
        # Verify user exists
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_list =session.exec(select(Dataset).select_from(join(DatasetACL, Dataset, DatasetACL.dataset_id == Dataset.id)).where(DatasetACL.accessgroup_id.in_(ag_ids))).all()

        return [ds.unique_id for ds in ds_list]


@app.get("/users/{orcid}/datasets/{dsid}", response_model = Dict)
def read_user_access_to_dataset(dsid: str, orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    '''
    Returns boolean: True is user with provided ORCID has access to the provided dataset ID, False if no access or dataset does not exist. 

    I'm not sure why logic is different than above ... could also call above endpoint and filter list?  Worth time check?
    '''
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

# @app.get("/users/{orcid}/projects", response_model = List[Project])
# def read_projects_for_user(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
#     '''
#     Returns a list of the projects that this User is associated with.
#     '''
#     with Session(engine) as session:
#         user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()

#         if user is None:
#             raise HTTPException(status_code=404, detail="User not found")

#         user_id = user.id
#         projects = session.exec(select(Project).select_from(join(Project, UserProjectLink, Project.project_id == UserProjectLink.project_id)).where(UserProjectLink.user_id == user_id)).all()
#         return projects
    

@app.get("/users/{orcid}/projects", response_model = List[Project])
def read_projects_for_user(orcid: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of the projects that this User is associated with.
    '''
    access_group_name, ag_ids = auth_res
    
    if not access_group_name.startswith('admin') and access_group_name != orcid:
        raise HTTPException(status_code = 403, detail="Not authorized to view projects for this user")
    
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()

        if user is None:
            raise HTTPException(status_code=404, detail="User not found")

        user_id = user.id
        projects = session.exec(select(Project).select_from(join(Project, UserProjectLink, Project.project_id == UserProjectLink.project_id)).where(UserProjectLink.user_id == user_id)).all()
        return projects
    
    
@app.post("/projects", response_model = Project)
def create_project(auth_res: Annotated[tuple, Depends(authorize_user)], P: Project):
    """
    Add or update a project record in the database.

    Args:
        auth_res: ACL info for user
        P: Project object to be added or updated

    Returns:
        Added or updated project object

    Raises:
        HTTPException: If required project fields are missing
    """
    # Validate required fields before attempting SQL operations
    missing_fields = []
    if not P.project_id or not P.project_id.strip():
        missing_fields.append("project_id")
    if not P.organization or not P.organization.strip():
        missing_fields.append("organization")
    if not P.project_lead_email or not P.project_lead_email.strip():
        missing_fields.append("project_lead_email")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required project information: {', '.join(missing_fields)}. Please provide all required fields: project_id, organization, and project_lead_email."
        )

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
def list_projects(auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of projects the user is authorized to see
    '''
    access_group_name, ag_ids = auth_res
    if access_group_name.startswith('admin'):
        with Session(engine) as session:
            props = session.exec(select(Project)).all()
            return(props)
        
    elif is_orcid(access_group_name):
        props = read_projects_for_user(orcid = access_group_name,
                                       auth_res = (access_group_name, ag_ids))
        return props
    
    else:
        raise HTTPException(status_code = 403, detail = "Not authorized to view projects")

@app.get("/projects/{proj_id}", response_model = Union[None, Project])
def read_project(proj_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the project object for the provided ID
    '''
    with Session(engine) as session:
        prop = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        # should check user is in prop group
        return(prop)


@app.get("/projects/{proj_id}/users", response_model = List[User])
def read_users_on_project(proj_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of users associated with the project ID provided
    '''
    with Session(engine) as session:
        # Verify project exists
        project = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()

        if project is None:
            raise HTTPException(status_code=404, detail="Project not found")

        users = session.exec(select(User).select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id)).where(UserProjectLink.project_id == proj_id)).all()
        return users


@app.post("/projects/{proj_id}/users/{orcid}", response_model = List[User])
def add_users_to_project(proj_id: str, orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):

    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        if user:
            # add link between user and project
            link = add_or_update(session, 
                             UserProjectLink,
                             where = (and_(UserProjectLink.user_id == user.id, UserProjectLink.project_id == proj_id)), 
                             user_id = user.id, 
                             project_id = proj_id)
            session.commit()
        
        # add user to access group
        add_user_to_access_group(admin_acct = admin_acct, orcid = orcid, group_name = proj_id)

        project_users = read_users_on_project(proj_id, admin_acct)
        return project_users


@app.post("/samples/{parent_sample_id}/children/{child_sample_id}", response_model = SampleLink)
def link_parent_child(parent_sample_id, child_sample_id):

    with Session(engine) as session:
        parent_sample = session.exec(select(Sample).where(Sample.unique_id == parent_sample_id)).first()
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).first()
        link = add_or_update(session, 
                     SampleLink,
                     where = (and_(SampleLink.parent_sample_id == parent_sample.id, SampleLink.sample_id ==child_sample.id)), 
                     parent_sample_id = parent_sample.id, 
                     sample_id = child_sample.id)
        
        session.commit()
        session.refresh(link)
        
        return(link)


@app.patch('/samples/{sample_id}', response_model = Sample)
def update_sample(auth_res: Annotated[tuple, Depends(authorize_user)], sample_id: str, updated_sample: Sample):
    access_group_name, ag_ids = auth_res
    cmd = select(Sample).where(Sample.unique_id == sample_id)
    update_fields = updated_sample.model_dump(exclude_unset=True)
    with Session(engine) as session:
        sample = session.exec(cmd).one_or_none()
        for key, value in update_fields.items():
            setattr(sample, key, value)
            print(key,value)
        session.add(sample)
        session.commit()
        session.refresh(sample)
        return sample
    
    if not sample:
        raise HTTPException(status_code = 404, detail = 'Sample not found')




@app.post("/samples", response_model = Sample)
def create_sample(auth_res: Annotated[tuple, Depends(authorize_user)], S: Sample):
    """
    Create a new sample in the database.

    Args:
        auth_res: Authorization result containing access group info
        S: Sample object to be added

    Returns:
        Added sample object

    Raises:
        HTTPException: If required sample fields are missing
    """
    access_group_name, ag_ids = auth_res

    # Validate required fields
    missing_fields = []
    if not S.sample_name or not S.sample_name.strip():
        missing_fields.append("sample_name")

    if missing_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Missing required sample information: {', '.join(missing_fields)}. Please provide all required fields: sample_name."
        )

    with Session(engine) as session:
        print("starting sample addition")
        where = or_(Sample.unique_id == S.unique_id, 
                and_(Sample.project_id == S.project_id, 
                     Sample.sample_name == S.sample_name))
        statement = select(Sample).where(where)
        sample = session.exec(statement).one_or_none()

        print(f'{sample=}')
        if sample: 
            return sample
        
        sample = Sample(**S.model_dump(exclude_unset = True, exclude = {"parents", "children"}))
        print(f'{sample=}')
        session.add(sample)
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
def add_sample_to_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str, sample_id:str):
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

        
        add_or_update(session,
                      DatasetSampleLink,
                      where=(and_(DatasetSampleLink.sample_id == samp.id, DatasetSampleLink.dataset_id == ds.id)),
                      sample_id = samp.id,
                      dataset_id = ds.id
                      )
        
        session.commit()

        info = DatasetSampleLinkResponse(dataset_id = ds.unique_id, dataset_name = ds.dataset_name, sample_id = samp.unique_id, sample_type = samp.sample_type, sample_name = samp.sample_name)
    return info


@app.get("/samples", response_model = List[SampleWithDatasetsResponse])
def list_samples(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    '''
    Returns a list of samples where the owner_orcid matches the orcid of the person who made the request.
    If an admin API key is used, returns all samples.
    '''
    access_group_name, ag_ids = auth_res

    query_params = request.query_params
    print(query_params)
    
    params_dict = dict(query_params)
    print(params_dict)
    
    with Session(engine) as session:
        
        samp_cmd = select(Sample)
        
        # Admin API keys can see all samples
        if access_group_name.startswith('admin'):
            # No owner filter for admin - they can see all samples
            print('using admin account')
            pass
        # Filter by owner_orcid matching requester's ORCID (access_group_name when using ORCID-based API key)
        elif is_orcid(access_group_name):
            print(f'filtering on owner_orcid={access_group_name}')
            associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
            associated_group_names = [ag.group_name for ag in associated_groups]
            samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == access_group_name,
                                          Sample.project_id.in_(associated_group_names)))
        else:
            # If not an ORCID-based request and not admin, return empty list for security
            print(f'apikey does not have access to samples')
            return []
    
        for k, v in params_dict.items():
            if hasattr(Sample, k) and v is not None:
                print(f'filtering results on {k}={v}')
                samp_cmd = samp_cmd.where(getattr(Sample, k) == v)
            else:
               pass
            
        samp_cmd = samp_cmd.options(selectinload(Sample.datasets))
        samp_list = session.exec(samp_cmd).all()
        return samp_list

@app.get("/projects/{proj_id}/sample_graph", response_model = Union[None, Project])
def project_sample_graph(project_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):

    access_group_name, ag_ids = auth_res

    with Session(engine) as session:
        
        samp_cmd = select(Sample.unique_id, SampleLink.sample_id).select_from(join(Sample,SampleLink, Sample.id == SampleLink.parent_sample_id))
        
        # Admin API keys can see all samples
        if access_group_name.startswith('admin'):
            # No owner filter for admin - they can see all samples
            print('using admin account')
            pass
        # Filter by owner_orcid matching requester's ORCID (access_group_name when using ORCID-based API key)
        elif is_orcid(access_group_name):
            print(f'filtering on owner_orcid={access_group_name}')
            associated_groups = session.exec(select(AccessGroup).where(AccessGroup.id.in_(ag_ids))).all()
            associated_group_names = [ag.group_name for ag in associated_groups]
            samp_cmd = samp_cmd.where(or_(Sample.owner_orcid == access_group_name,
                                          Sample.project_id.in_(associated_group_names)))
        else:
            # If not an ORCID-based request and not admin, return empty list for security
            print(f'apikey does not have access to samples')
            return []
    
        # Filter samples on proj_id
        samp_cmd = samp_cmd.where(Sample.project_id==project_id)

        # for k, v in params_dict.items():
        #     if hasattr(Sample, k) and v is not None:
        #         print(f'filtering results on {k}={v}')
        #         samp_cmd = samp_cmd.where(getattr(Sample, k) == v)
        #     else:
        #        pass
        
        #samp_cmd = samp_cmd.options(selectinload(Sample.datasets))

        samp_list = session.exec(samp_cmd).all()
        return samp_list


@app.get("/samples/{sample_id}", response_model = Union[None, SampleWithDatasetsResponse])
def read_sample(sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns the sample for the provided ID
    '''
    with Session(engine) as session:
        query = select(Sample).options(selectinload(Sample.datasets)).where(Sample.unique_id == sample_id)
        samp = session.exec(query).first()
        return(samp)


@app.get("/datasets/{dsid}/samples", response_model = List[Sample])
def read_samples_in_dataset(dsid: str, auth_res:  Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of samples associated with the dataset ID provided
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)).where(Dataset.unique_id == dsid).filter((DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")
        
        samples = session.exec(select(Sample).select_from(join(Sample, DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)).where(DatasetSampleLink.dataset_id == ds.id)).all()
        return samples


@app.get("/samples/{sample_id}/datasets")
def list_sample_datasets(sample_id: str, auth_res:  Annotated[tuple, Depends(authorize_user)], include_metadata: bool = False):
    '''
    Returns a list of all datasets linked to a given sample
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Verify sample exists
        sample = session.exec(select(Sample).where(Sample.unique_id == sample_id)).one_or_none()

        if sample is None:
            raise HTTPException(status_code=404, detail="Sample not found")

        auth_filter = (DatasetACL.accessgroup_id.in_(ag_ids)) | (Dataset.public == True)
        print(f'{include_metadata=}')
        if include_metadata:
            query = (
                    select(Dataset)
                    .options(selectinload(Dataset.scientific_metadata))
                    .join(DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                    .filter(auth_filter)
                )


        else:
            query = (select(Dataset)
                        .select_from(
                            join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id, isouter=True)
                        )
                        .filter(auth_filter)
                    )
        
        full_query = query.join(DatasetSampleLink,
                                DatasetSampleLink.dataset_id == Dataset.id
                                ).where(
                                    DatasetSampleLink.sample_id == select(Sample.id).where(Sample.unique_id == sample_id)
                                ).distinct(Dataset.id)
        datasets = session.exec(full_query).all()
        if include_metadata:
            return [{**dataset.model_dump(),
                    "scientific_metadata": dataset.scientific_metadata.model_dump() if dataset.scientific_metadata else None
                    }
                    for dataset in datasets]
        else:
            return datasets
    


@app.get("/samples/{child_sample_id}/parents", response_model = List[Sample])
def list_parent_samples(child_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of child samples for a given parent sample
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        # Get parent sample
        child_sample = session.exec(select(Sample).where(Sample.unique_id == child_sample_id)).one_or_none()
        
        if child_sample is None:
            raise HTTPException(status_code=404, detail="Child sample not found")
        
        # Get child samples
        parent_samples = session.exec(
            select(Sample)
            .select_from(join(Sample, SampleLink, SampleLink.parent_sample_id == Sample.id))
            .where(SampleLink.sample_id == child_sample.id)
        ).all()
        return parent_samples
    

@app.get("/samples/{parent_sample_id}/children", response_model = List[Sample])
def list_child_samples(parent_sample_id: str, auth_res: Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of child samples for a given parent sample
    '''
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
    """
    Get metadata for a sample based on sample_type.
    
    Args:
        auth_res: Authorization result containing access group info
        sample_id: Unique identifier for the sample
        sample_type: Type of sample metadata (e.g., 'spinbot_batch')
        
    Returns:
        Sample metadata object if found
        
    Raises:
        HTTPException: If sample not found, invalid sample_type, or metadata not found
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
    """
    Create or update metadata for a sample based on sample_type.
    
    Args:
        auth_res: Authorization result containing access group info
        sample_id: Unique identifier for the sample
        sample_type: Type of sample metadata (e.g., 'spinbot_batch')
        metadata: Dictionary containing metadata fields
        
    Returns:
        Created/updated metadata object
        
    Raises:
        HTTPException: If sample not found, invalid sample_type, or validation errors
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
    