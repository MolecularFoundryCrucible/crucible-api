from typing import Annotated, List, Tuple, Dict, Optional, Union
import json
import yaml
import requests
from jose import jwt
import urllib.parse
import datetime
import pytz
import os
from pathlib import Path
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File, Body
from starlette.requests import Request
from starlette.background import BackgroundTasks
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.security import HTTPBearer


from sqlmodel import Session, select
from sqlalchemy import join, func,text
from sqlalchemy.orm import selectinload, joinedload
from sqlalchemy.sql import and_

from app.general_utils import get_secret, run_rclone_command, remove_file, get_tz_isoformat, is_orcid
from app.utils.sql_utils import setup_engine, add_or_update, only_update, table_obj_to_dict
from app.utils.rmq_utils import send_pika_message


from app.models.core_models import (Dataset, 
                                    DatasetACL,
                                    Keyword,
                                    DatasetKeywordLink,
                                    Thumbnail,
                                    AssociatedFile,
                                    User,
                                    AccessGroup,
                                    Project,
                                    UserAccessGroupLink,
                                    UserProjectLink,
                                    Sample,
                                    SampleLink,
                                    DatasetSampleLink,
                                    DatasetCollection,
                                    DatasetCollectionLink,
                                    ScientificMetadata)

from app.models.instrument import Instrument
from app.models.misc import IdType, DataDriveLocation, GoogleDrives
from app.models.tasks import GoogleDriveTransferRequest, SciCatUpdateRequest, IngestionRequest
from app.models.response_schemas import SampleWithDatasetsResponse

# import argparse

# parser = argparse.ArgumentParser()
# parser.add_argument('--db_config', help='Database config file', required=False)
# parser.add_argument('--api_config', help='API config file', required = False)
# args = parser.parse_args()

# ======================= APP
app = FastAPI()
security = HTTPBearer()


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
orcid_client_id = apiconf['orcid_client_id']
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
def redirect_exchange(request: Request, code: str, state: str=None):
    """
    Exchange the ORCID authorization code for an access token and user information.
    
    Args:
        request: Request object containing the HTTP request
        code: ORCID authorization code
        state: Optional state parameter (default is None)
        
    Returns:
        RedirectResponse to the appropriate page after successful authentication
    """
    from app.utils.auth_utils import SECRET_KEY, ALGORITHM, code_for_token_exchange
    token = code_for_token_exchange(code, orcid_client_id, orcid_client_secret, f"{api_url}/auth")                                
    user_from_orcid = requests.get(url = "https://orcid.org/oauth/userinfo", headers = {"Authorization": f"Bearer {token}"})
    try:
        user = user_from_orcid.json()
        exp_time = datetime.datetime.utcnow() + datetime.timedelta(minutes=1)
        user_info_payload = { "orcid": user["sub"], "exp": exp_time }
        user_info_jwt = jwt.encode(user_info_payload, SECRET_KEY, algorithm=ALGORITHM)
        
    except Exception as err:
        print(err)
        user_info_jwt = None
    next_page = request.cookies.get("after_login_return_to_page")
    if next_page == 'None':
        response = RedirectResponse("/")
    else:
        response = RedirectResponse(next_page)
    response.set_cookie("after_login_return_to_page", value = None, httponly = True, secure = True)
    response.set_cookie("crucible_user_token",
                         value=user_info_jwt,
                         httponly=True,
                         secure=True,
                         max_age=60 )
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
        response.set_cookie("after_login_return_to_page", value = f"/{api_path}/user_apikey", httponly = True, secure = True)
        return response
    
    user_info = decode_token(user_info_jwt)
    orcid = user_info['orcid']
    crucible_token = find_or_add_api_access(orcid, engine, regenerate = False)
    return(f"Your Crucible APIkey is: {crucible_token}")


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
        response.set_cookie("after_login_return_to_page", value = f"/{api_path}/regenerate_apikey", httponly = True, secure = True)
        return response
            
    user_info = decode_token(user_info_jwt)
    orcid = user_info['orcid']
    crucible_token = find_or_add_api_access(orcid, engine, regenerate = True)

    return(f"Your New Crucible APIkey is: {crucible_token}")



######################## =============================DATASETS ==========================================================================================================  

# TODO: person creating dataset must exist in create data set access group
# TODO: public datasets rate limited but dont require orcid
# TODO: use agids to authorize for all endpoints
@app.post("/datasets", response_model = Dataset)
async def create_dataset(auth_res:  Annotated[tuple, Depends(authorize_user)],
                         dataset: Dataset):
    
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
        admin_acl = sql_create_ds_acl(admin, ds, read = True, write = True, session=session)

        ro = session.exec(select(AccessGroup).where(AccessGroup.group_name == "read_only")).one_or_none()
        ro_acl = sql_create_ds_acl(ro, ds, read = True, write = False, session=session)
        print("add ro access")

        '''
        All this logic is so backward because we literally
        have the instrument name and orcid then we do get 
        requests to get the "IDs" now we have to do the get 
        requests AGAIN to get the information we actually want 
        and had the whole time  
        '''
        
        if dataset.owner_user_id is not None:
            print('adding owner orcid access')
            user_orcid = session.exec(select(User.orcid).where(User.id == dataset.owner_user_id)).one_or_none()
            user_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user_orcid)).one_or_none()
            user_acl = sql_create_ds_acl(user_ag, ds, read = True, write = True, session=session)
            print('added owner orcid access')

        else:
            # use the apikey associated accessgroup as owner
            creator = session.exec(select(AccessGroup).where(AccessGroup.group_name == access_group_name)).one_or_none()
            creator_acl = sql_create_ds_acl(creator, ds, read = True, write = True, session=session)
            print('add creator access')

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

    if ds_list is None:
        raise HTTPException(status_code=402,
                            detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=402,
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


@app.get("/datasets", response_model = List[Dataset])
def list_datasets(auth_res: Annotated[tuple, Depends(authorize_user)], request: Request):
    '''
    Returns a list of all dataset IDs the requesting user is authorized to view. 
    If no dataset IDs are authorized for the user, an empty list will be returned. 
    '''
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
                                                      DatasetACL.dataset_id == Dataset.id),
                                                      DatasetKeywordLink.dataset_id == Dataset.id)).filter(
                                                      DatasetACL.accessgroup_id.in_(ag_ids)).filter(
                                                      DatasetKeywordLink.keyword_id.in_(k_ids))
        
        del params_dict['keyword']

        
    else:
        ds_cmd = select(Dataset).select_from(
                                                join(Dataset, 
                                                     DatasetACL,
                                                     DatasetACL.dataset_id == Dataset.id)
                                                ).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    
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
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        return ds



@app.delete("/datasets/{dsid}", response_model = Dataset)
def delete_dataset(auth_res: Annotated[tuple, Depends(authorize_user)], dsid = str):
    access_group_name, ag_ids = auth_res
    # TODO: DEFINE THIS
    pass



@app.get("/datasets/{dsid}/download/{file_name}")
async def download_dataset_files(auth_res:  Annotated[tuple, Depends(authorize_user)], dsid: str, file_name: str):
    '''
    Download an associated file directly from mounted GCS storage without copying.
    Doing this by file_name for now after discussing with Ed. This is the most 
    intuitive and human readable. This solution also avoids exposing the internal 
    ID which is the only feature of the AF that is currently guaranteed unique.  
    The file_name is actually a relative path which reduces the "intuition" 
    fairly drastically in the case that your "dataset" has subdirectories of 
    associated files. 
    '''
    access_group_name, ag_ids = auth_res
    print("got auth")
    
    # check dataset access
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, 
                                              DatasetACL.dataset_id == Dataset.id)
                                        ).where(Dataset.unique_id == dsid
                                               ).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
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
    '''
    Given an existing dataset record - adds data from an uploaded file
    - makes user call 2 endpoints 1) /datasets 2) /datasets/{dsid}/upload
    - json info added during step 1
    - If dsid in file then its a little convoluted ... but you could 
      always pull it out and then send it in first request json
    '''
    
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
        raise HTTPException(status_code=402, detail="Dataset not found")
    
    elif file_to_upload is None:
        file_to_upload = ds.file_to_upload

    else:
        ds.file_to_upload = file_to_upload

    if not file_to_upload:
        raise HTTPException(status_code=402, detail="No file listed to ingest")

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
    '''
    For now keep simple - Input is a dataset.
    Drive info is derived from Project and Owner.
    Location within drive is hardcoded.  
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        pst= pytz.timezone("America/Los_Angeles")
        creation_time = datetime.datetime.now(pst).isoformat()

        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
        with Session(engine) as session:
            ds_list = session.exec(ds_cmd).all()
    
        if ds_list is None:
            raise HTTPException(status_code=402, detail="Dataset not found")
        elif len(ds_list) == 0:
            raise HTTPException(status_code=402, detail="Dataset not found")
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
    pst= pytz.timezone("America/Los_Angeles")
    creation_time = datetime.datetime.now(pst).isoformat()
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=402, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=402, detail="Dataset not found")
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
    if sc_req.id:
        assert reqid == sc_req.id
    with Session(engine) as session:
        updated_req = only_update(session, SciCatUpdateRequest, (SciCatUpdateRequest.id == sc_req.id) & (SciCatUpdateRequest.dataset_id == dsid), **sc_req.model_dump(exclude_unset=True))
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.get("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def read_scientific_metadata(auth_res: Annotated[tuple, Depends(authorize_user)], dsid: str):
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
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
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_thumbnails = session.exec(select(Thumbnail).where(Thumbnail.dataset_id == ds.id)).all()
        return ds_thumbnails


@app.post("/datasets/{dsid}/thumbnails", response_model = Thumbnail)
def add_thumbnail(auth_res: Annotated[tuple, Depends(authorize_user)], 
                  dsid: str, 
                  thumbnail: Thumbnail):
    '''
    Adds a thumbnail to a dataset. If the thumbnail exists, it will simply add the existing thumbnail to the dataset object. 
    Otherwise, the thumbnail will be created and then added to the dataset. 
    '''
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
    '''
    Returns a list of the files associated with the dataset ID provided. 
    '''
    access_group_name, ag_ids = auth_res
    
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_files = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)).all()
        return ds_files


@app.post("/datasets/{dsid}/associated_files", response_model = AssociatedFile)
def add_associated_file(auth_res: Annotated[tuple, Depends(authorize_user)], 
                        dsid: str, 
                        associated_file: AssociatedFile):
    '''
    Adds an associated file to a dataset. 
    Creates the AF object if it doesn't already exist.
    '''
    print("accepted args")
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        af_obj = add_or_update(session, AssociatedFile, where=(AssociatedFile.filename == associated_file.filename),
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
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
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
    with Session(engine) as session:
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

    """
    access_group_name, ag_ids = auth_res
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=402, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=402, detail="Dataset not found")
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
        ags = session.exec(select(AccessGroup).where(AccessGroup.id).in_(ag_ids)).all()
        return([ag.group_name for ag in ags])



# =====================

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

    statement = select(Instrument)
    for k,v, in params_dict.items():
        if hasattr(Instrument, k):
            statement = statement.where(getattr(Instrument, k) == v.lower().strip())
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
        drive = session.exec(select(GoogleDrives).where(GoogleDrives.drive_name == drive_name)).all()

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
    '''
    Returns a list of all the datasets the user has access to based on the ORCID provided. 
    '''
    with Session(engine) as session:
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

@app.get("/users/{orcid}/projects", response_model = List[Project])
def read_projects_for_user(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of the projects that this User is associated with. 
    '''
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
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
    '''
    Returns the project object for the provided ID
    '''
    with Session(engine) as session:
        props = session.exec(select(Project)).all()
        return(props)


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
        users = session.exec(select(User).select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id)).where(UserProjectLink.project_id == proj_id)).all()
        return users
        
@app.post("/samples/{parent_sample_id}/children/{child_sample_id}", response_model = SampleLink)
def link_parent_child(parent_sample_id, child_sample_id):

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


@app.post("/datasets/{dsid}/samples/{sample_id}")
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

        newlink = DatasetSampleLink(sample_id = samp.id, dataset_id = ds.id)
        session.add(newlink)
        session.commit()

    return newlink


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
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        
        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found or access denied")
        
        samples = session.exec(select(Sample).select_from(join(Sample, DatasetSampleLink, DatasetSampleLink.sample_id == Sample.id)).where(DatasetSampleLink.dataset_id == ds.id)).all()
        return samples


@app.get("/samples/{sample_id}/datasets", response_model = List[Dataset])
def list_sample_datasets(sample_id: str, auth_res:  Annotated[tuple, Depends(authorize_user)]):
    '''
    Returns a list of all datasets linked to a given sample
    '''
    access_group_name, ag_ids = auth_res
    with Session(engine) as session:
        datasets = session.exec(
            select(Dataset)
            .select_from(
                join(Dataset, DatasetSampleLink, DatasetSampleLink.dataset_id == Dataset.id)
                .join(DatasetACL, DatasetACL.dataset_id == Dataset.id)
            )
            .where(DatasetSampleLink.sample_id == 
                   select(Sample.id).where(Sample.unique_id == sample_id)
            )
            .filter(DatasetACL.accessgroup_id.in_(ag_ids))
            .distinct()
        ).all()
        return datasets


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
    