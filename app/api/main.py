from typing import Annotated, List, Tuple, Dict, Optional, Union
import json
import yaml
import requests
from jose import jwt
import urllib.parse
import datetime
import pytz
import os

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File, Body
from starlette.requests import Request
from starlette.background import BackgroundTasks
from fastapi.responses import RedirectResponse, FileResponse
from fastapi.security import HTTPBearer


from sqlmodel import Session, select
from sqlalchemy import join, func
from sqlalchemy.sql import and_

from app.general_utils import get_secret, run_rclone_command, remove_file, get_tz_isoformat
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
                                    DatasetCollection,
                                    DatasetCollectionLink,
                                    ScientificMetadata)

from app.models.instrument import Instrument
from app.models.misc import IdType, DataDriveLocation, GoogleDrives
from app.models.tasks import GoogleDriveTransferRequest, SciCatUpdateRequest, IngestionRequest

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
orcid_client_secret = get_secret("CLIENT_SECRET", "projects/776258882599/secrets/orcid_client_secret/versions/2")
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
async def create_dataset(ag_ids:  Annotated[str, Depends(authorize_user)],
                         dataset: Dataset):
    """
    Create a new dataset in Crucible by provided a dataset object and optionally the file that you wish to create the dataset from. 
    If a file is provided the contents will be uploaded to the crucible-uploads bucket and saved to the relative path defined by filename. 
    This path will be used as the "file_to_upload" field in the Dataset object **and will override the existing value. 

    The /datasets/{dsid}/ingest endpoint will then be called using the resulting dataset object and unique_id. 

    The max size of a file is currently set to 500MB. If files are larger than 500MB then an empty dataset can be created through this endpoint using a blank Dataset object with no "file_to_upload" specified. 
    Data can then be streamed to a primary_file (should replace the file_to_upload field and get combined with extract_data) using the /datasets/{dsid}/stream endpoint. 

    Args:
        dataset: A Crucible Dataset object. 
            - If provided with no attributes defined, it will be created with an autogenerated MFID and public set to false.  
            - Can contain a fully defined dataset
            - Results in a new SQL dataset record
            - If file_to_upload is defined; the ingestion process will be triggered with RMQ for that file.  **This can get updated using notes from commit 1a7c1e1 on main

        file: Optional file to be uploaded
        ag_ids: List of access group IDs the user belongs to; used to confirm they have permission to upload the file
        
    Returns:
        Returns the Dataset object that was created. 
    
    Data and metadata can be added to a dataset at the time of or after the record creation.
    
    By default it will give access to the admin and readonly accounts. If user information, project, or instrument are included, 
    then access will also be given to the relevant groups. 

    The dataset ID is recorded in a table to track that the mfid corresponds to a dataset. 
    """

    # if file:
    #     # save the file to crucible-uploads
    #     save_file_path = f"/root/uploads/{file.filename}"
    #     with open(save_file_path, "wb") as f:
    #         f.write(await file.read())
    #     dataset.file_to_upload == file.filename


    # apikey = get_secret("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/3")
    with Session(engine) as session:

        #newds = table_obj_to_dict(dataset)
        ds = add_or_update(session, Dataset, Dataset.unique_id == dataset.unique_id, **dataset.model_dump(exclude_unset = True))
        print(ds)
        print("added ds")
        session.commit()
        print("committed")
        
        # set appropriate access controls
        admin = session.exec(select(AccessGroup).where(AccessGroup.group_name == "admin")).one_or_none()
        admin_acl = sql_create_ds_acl(admin, ds, read = True, write = True, session=session)
        print('add admin access')

        ro = session.exec(select(AccessGroup).where(AccessGroup.group_name == "read_only")).one_or_none()
        ro_acl = sql_create_ds_acl(ro, ds, read = True, write = False, session=session)
        print("add ro access")

        #All this logic is so backward because we literally have the instrument name and orcid then we do get requests to get the "IDs" now we have to do the get requests AGAIN to get the information we actually want and had the whole time
        if dataset.owner_user_id is not None:
            print('adding owner orcid access')
            user_orcid = session.exec(select(User.orcid).where(User.id == dataset.owner_user_id)).one_or_none()
            user_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user_orcid)).one_or_none()
            user_acl = sql_create_ds_acl(user_ag, ds, read = True, write = True, session=session)
            print('added owner orcid access')

        if dataset.project_id is not None:
            print('adding proposal access')
            prop_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == dataset.project_id)).one_or_none()
            prop_acl = sql_create_ds_acl(prop_ag, ds, read = True, write = True, session=session)
            print('added proposal access')

        if dataset.instrument_id is not None:
            print('adding instrument group access')
            inst_name = session.exec(select(Instrument.instrument_name).where(Instrument.id == dataset.instrument_id)).one_or_none()
            inst_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == inst_name.lower())).one_or_none()
            if inst_ag is not None:
                inst_acl = sql_create_ds_acl(inst_ag, ds, read = True, write = True, session=session) 
            else:
                inst_ag = add_or_update(session, AccessGroup, AccessGroup.group_name == inst_name.lower(), group_name = inst_name.lower())
                inst_acl = sql_create_ds_acl(inst_ag, ds, read = True, write = True, session=session)
            print('added instrument group access')

        add_or_update(session, IdType, where=(IdType.unique_id == dataset.unique_id), unique_id = dataset.unique_id, object_type = "dataset")
        print('added the ID type')
        session.commit()
        print('committed')
        session.refresh(ds)

    # if dataset.file_to_upload is not None:
    #     # call ingest endpoint 
    #     print('connecting to ingestion endpoint')
    #     payload  = {"dataset": dataset}

    #     ds = requests.post(url = f"{api_url}/datasets/{dataset.unique_id}/ingest",  
    #                         headers = {"Authorization":f"Bearer {apikey}"}, 
    #                         data = payload   #TODO: check this request format
    #                       )
    #     print('sent ingestion request')

    return ds


@app.patch("/datasets/{dsid}", response_model = Dataset)
def update_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, updated_dataset: Dataset):
    '''
    Updates a dataset in SQL and returns the Updated Dataset object. 
    Sends a message to RabbitMQ to alert RMQ that updates have been made.
    '''
    #newds = table_obj_to_dict(updated_dataset)
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
        stored_ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        if stored_ds:   
            stored_ds_dict = dict(stored_ds)
        print(f"{stored_ds_dict=}")

        update_attr = updated_dataset.model_dump(exclude_unset=True)
        print(f"{update_attr=}")
        filtered_updates = {k:v for k,v in update_attr.items() if stored_ds_dict[k] == None}
        print(f"{filtered_updates=}")
        #Is this going to work with add or update? yes : )
        upds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **filtered_updates)


        if upds.owner_user_id is not None:
            user_orcid = session.exec(select(User.orcid).where(User.id == updated_dataset.owner_user_id)).one_or_none()
            user_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user_orcid)).one_or_none()
            user_acl = sql_create_ds_acl(user_ag, upds, read = True, write = True, session=session)
            print('add owner orcid access')

        if upds.project_id is not None:
            prop_name = session.exec(select(Project.project_id).where(Project.project_id == updated_dataset.project_id)).one_or_none()
            prop_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == prop_name)).one_or_none()
            prop_acl = sql_create_ds_acl(prop_ag, upds, read = True, write = True, session=session)
            print('add proposal access')

        if upds.instrument_id is not None:
            inst_name = session.exec(select(Instrument.name).where(Instrument.id == updated_dataset.instrument_id)).one_or_none()
            inst_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == inst_name)).one_or_none()
            inst_acl = sql_create_ds_acl(inst_ag, upds, read = True, write = True, session=session) 
            print('add instrument group access')
        
        session.commit()
        session.refresh(upds)
        '''
        message = json.dumps(dict(ds))
        rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
    
        TODO: check how dsupdate and dsupdate-owner consumers differ / what are the inputs expected --- can we consolidate?
        
        ds update needs to be separate ...............
            - This endpoint is getting called by the regular ingest function when it extracts info from a file
            - Ingestion consumer calls transfer / scicat endpoints, but maybe it shouldn't ...
            - Change to ds record in DB results in a transfer / scicat update ...
             OR
            - If you are updating a ds record with new info not coming from ingestion consumer - you need to explicitly call transfer and scicat endpoints your self... I like that for now. 
         
        note: dsupdate-owner is expected the message to contain the field updated_ownership_info whereas dsupdate is expected updated_info
              both are expecting "dataset_to_update": pid
              update_info can be any dictionary
              updated_ownership_info is an instance of the Owner schema
        Really this will have to change to work but the current format would look like: 
        message = json.dumps({"dataset_to_update": updated_dataset.unique_id, "updated_info": dict(updated_dataset)})
        and for ownership it would look like: 
        message = json.dumps({"dataset_to_update": updated_dataset.unique_id, "updated_ownership_info": dict(Owner(subset of updated_dataset))})
        send_pika_message(message, exchange = "", routing_key= "dsupdate-newapi", user = 'admin', rmq_pw= rmq_pw)
        '''
        return upds


@app.get("/datasets", response_model = List[str])
def list_datasets(ag_ids: Annotated[str, Depends(authorize_user)], request: Request):
    '''
    Returns a list of all dataset IDs the requesting user is authorized to view. 
    If no dataset IDs are authorized for the user, an empty list will be returned. 

    '''
    query_params = request.query_params
    print(query_params)
    params_dict = dict(query_params)

    if 'keyword' in list(params_dict.keys()):
        K = session.exec(select(Keyword).where(Keyword.keyword.ilike(f"%{v}%"))).all()
        if K:
            k_ids = [k.id for k in K]
            ds_cmd = select(Dataset).select_from(join(DatasetKeywordLink,
                                                join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id),
                                                     DatasetKeywordLink.dataset_id == Dataset.id))\
                                                .filter(DatasetACL.accessgroup_id.in_(ag_ids))\
                                                .filter(DatasetKeywordLink.keyword_id.in_(k_ids))
        
        del params_dict[k]

    else:
        ds_cmd = select(Dataset.unique_id).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    

    for k, v in params_dict.items():
        if hasattr(Dataset, k):
            ds_cmd = ds_cmd.where(getattr(Dataset, k) == v)

    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()
        return ds_list
    
    
@app.get("/datasets/{dsid}", response_model = Union[Dataset, None])
def read_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a dataset found with the provided unique ID if one is exists and the requesting user is authorized to view the dataset. 
    Otherwise, returns None
    '''
    print(f"{ag_ids=}")
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        return ds



@app.delete("/datasets/{dsid}", response_model = Dataset)
def delete_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid = str):
    # TODO: DEFINE THIS
    pass



@app.get("/datasets/{dsid}/download/{file_name}")
async def download_dataset_files(ag_ids:  Annotated[str, Depends(authorize_user)], dsid: str, file_name: str):
    '''
    Download an associated file.  Doing this by file_name for now after discussing with Ed. This is the most intuitive and human readable. 
    This solution also avoids exposing the internal ID which is the only feature of the AF that is currently guaranteed unique.  
    The file_name is actually a relative path which reduces the "intuition" fairly drastically in the case that your "dataset"
    has subdirectories of associated files. 
    '''
    tasks = BackgroundTasks()
    
    # check dataset access
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

    # get data from cloud storage
    if ds:
        rclone_out = run_rclone_command(source_path=f":gcs:/mf-storage-prod/{dsid}/{file_name}", destination_path=f"/tmp/data/{dsid}/{file_name}", cmd = "copyto", gcs_config_name= apiconf['gcs_config_name'])
        print(rclone_out.stdout)
        print(rclone_out.stderr)

    # return file as FileResponse
    download_path = f"/tmp/data/{dsid}/{file_name}"
    tasks.add_task(remove_file, download_path)
    if os.path.exists(download_path):
        return FileResponse(download_path, media_type="application/octet-stream", filename=file_name, background = tasks)
    else:
        raise HTTPException(status_code=404, detail="File not found for requested dataset ID.")


@app.post("/datasets/{dsid}/upload", response_model = IngestionRequest)
async def create_dataset_from_file_upload(file: Annotated[UploadFile, File(description="A file read as UploadFile")],
                                          ag_ids:  Annotated[str, Depends(authorize_user)],
                                          dsid: str):
    '''
    Given an existing dataset record - adds data from an uploaded file
    - makes user call 2 endpoints 1) /datasets 2) /datasets/{dsid}/upload
    - json info added during step 1
    - If dsid in file then its a little convoluted ... but you could always pull it out and then send it in first request json
    Calls ingestion request

    Better than the other way because you would have to use FormData instead of "Dataset" model to combo with File
    '''
    print(dsid)
    print(file)
    # upload the file
    save_file_path = f"/mnt/gcs/{file.filename}"
    with open(save_file_path, "wb") as f:
        f.write(await file.read())

    # get the dataset
    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        ds.file_to_upload = file.filename
        session.add(ds)
        session.commit()
        session.refresh(ds)
    # call ingest endpoint 
    print('connecting to ingestion endpoint')
    #payload  = {"dataset": dict(ds)}
    import time
    stime = time.time()
    ing_req = request_dataset_ingestion(ag_ids, dsid, ds.file_to_upload)
    tmp = time.time() - stime
    print(round(tmp,2))
    print('sent ingestion request')
    return(ing_req)


@app.post("/datasets/{dsid}/ingest", response_model = IngestionRequest)
def request_dataset_ingestion(ag_ids: Annotated[str, Depends(authorize_user)],
                              dsid: str,
                              file_to_upload: Optional[str]=None,
                              ingestion_class: Optional[str]=None):
    '''
    This sends a rabbitMQ message to the ingestion queue.
    The current ingestion queue is only expecting a "file_to_upload" value which this has enough information to provide. 

    TODO: 
      - Need to consider how the system works when a dsid exists INSIDE the file
    '''
    print("request received")
    # for this to work you should add the person who creates
    # the dataset ID to the access groups when they create the dataset
    ds_cmd = select(Dataset).select_from(
                                    join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(
                                        Dataset.unique_id == dsid).filter(
                                            DatasetACL.accessgroup_id.in_(ag_ids))

    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

    if ds is None:
        raise HTTPException(status_code=402, detail="Dataset not found")
    
    elif file_to_upload is None:
        file_to_upload = ds.file_to_upload

    else:
        pass

    if not file_to_upload:
        raise HTTPException(status_code=402, detail="No file listed to ingest")

    pst= pytz.timezone("America/Los_Angeles")
    creation_time = get_tz_isoformat()
    ingestion_request = IngestionRequest(dataset_id = dsid, time_created=creation_time, file_to_upload= file_to_upload, status = "requested")
    with Session(engine) as session:
        session.add(ingestion_request)
        session.commit()
        session.refresh(ingestion_request)
        print('committed')
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
    send_pika_message(message = json.dumps({"filename": file_to_upload,
                                            "reqid": ingestion_request.id,
                                            "dsid": dsid, 
                                            "ingestion_class":ingestion_class}), exchange= "", routing_key= "ingest-newapi", user = 'admin', rmq_pw= rmq_pw)
    return ingestion_request


@app.patch("/datasets/{dsid}/ingest/{reqid}", response_model = IngestionRequest)
def patch_ingestion_request(is_admin: Annotated[str, Depends(is_admin)], reqid: int, dsid: str, ing_req: IngestionRequest):
    if ing_req.id:
        print(reqid)
        print(ing_req.id)
        assert reqid == ing_req.id
    with Session(engine) as session:
        updated_req = only_update(session, IngestionRequest, (IngestionRequest.id == ing_req.id) & (IngestionRequest.dataset_id == dsid), **ing_req.model_dump(exclude_unset=True))
        if updated_req is None:
            raise HTTPException(status_code=400, detail="No ingestion request found for the provided dataset and request ID combination")
        
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.post("/datasets/{dsid}/google_drive_transfer", response_model = GoogleDriveTransferRequest)
def request_dataset_transfer_to_drive(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    For now keep simple - Input is a dataset. Drive info is derived from Project and Owner.  Location within drive is hardcoded.  
    '''

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
        rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
        
        send_pika_message(message, exchange = "", routing_key= "transfer-newapi", user = "admin", rmq_pw = rmq_pw)
       # send_pika_message(message, exchange = "raw-data-processing", routing_key= "#.transfer-request.#", user = 'admin', rmq_pw= rmq_pw)
        return(transfer_request)


@app.patch("/datasets/{dsid}/google_drive_transfer/{reqid}", response_model = GoogleDriveTransferRequest)
def patch_transfer_request(is_admin: Annotated[str, Depends(is_admin)], reqid: str, dsid: str, xfer_req: GoogleDriveTransferRequest):
    if xfer_req.id:
        assert reqid == xfer_req.id
    with Session(engine) as session:
        updated_req = only_update(session, GoogleDriveTransferRequest, (GoogleDriveTransferRequest.id == xfer_req.id) & (GoogleDriveTransferRequest.dataset_id == dsid), **xfer_req.model_dump(exclude_unset=True))
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.post("/datasets/{dsid}/scicat_update", response_model = SciCatUpdateRequest)
def request_update_scicat_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
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

    scicat_request = SciCatUpdateRequest(dataset_id = dsid, time_created = creation_time, status = "requested")
    session.add(scicat_request)
    session.commit()
    session.refresh(scicat_request)

    message = json.dumps({"dsid": dsid, "source_bucket":"mf-storage-prod", "reqid": scicat_request.id})
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")

    send_pika_message(message, exchange = "", routing_key= "scicat-newapi", user = 'admin', rmq_pw= rmq_pw)
    #send_pika_message(message, exchange = "raw-data   -processing", routing_key= "#.scicat-request.#", user = 'admin', rmq_pw= rmq_pw)
    return(scicat_request)


@app.patch("/datasets/{dsid}/scicat_update/{reqid}", response_model = SciCatUpdateRequest)
def patch_scicat_request(is_admin: Annotated[str, Depends(is_admin)], reqid: str, dsid: str, sc_req: SciCatUpdateRequest):
    if sc_req.id:
        assert reqid == sc_req.id
    with Session(engine) as session:
        updated_req = only_update(session, SciCatUpdateRequest, (SciCatUpdateRequest.id == sc_req.id) & (SciCatUpdateRequest.dataset_id == dsid), **sc_req.model_dump(exclude_unset=True))
        session.commit()
        session.refresh(updated_req)
    return(updated_req)


@app.get("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def read_scientific_metadata(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_md = session.exec(select(ScientificMetadata).where(ScientificMetadata.dataset_unique_id == ds.unique_id)).one_or_none()
        return ds_md


@app.post("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def add_scientific_metadata(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, scientific_md: dict):
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
    
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")

        md_obj = add_or_update(session, ScientificMetadata, where=(ScientificMetadata.dataset_unique_id == dsid), scientific_metadata = scientific_md)

        session.commit()
        session.refresh(md_obj)
        return(md_obj)


@app.patch("/datasets/{dsid}/scientific_metadata", response_model = ScientificMetadata)
def update_scientific_metadata(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, scientific_md_updates: dict):
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
def read_thumbnails(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of thumbnail objects associated with the dataset ID provided
    '''
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_thumbnails = session.exec(select(Thumbnail).where(Thumbnail.dataset_id == ds.id)).all()
        return ds_thumbnails


@app.post("/datasets/{dsid}/thumbnails", response_model = Thumbnail)
def add_thumbnail(ag_ids: Annotated[str, Depends(authorize_user)], 
                  dsid: str, 
                  thumbnail: Thumbnail):
    '''
    Adds a thumbnail to a dataset. If the thumbnail exists, it will simply add the existing thumbnail to the dataset object. 
    Otherwise, the thumbnail will be created and then added to the dataset. 
    '''
    print('accepted args')
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
def read_associated_files(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of the files associated with the dataset ID provided. 
    '''
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_files = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)).all()
        return ds_files


@app.post("/datasets/{dsid}/associated_files", response_model = AssociatedFile)
def add_associated_file(ag_ids: Annotated[str, Depends(authorize_user)], 
                        dsid: str, 
                        associated_file: AssociatedFile):
    '''
    Adds an associated file to a dataset. 
    Creates the AF object if it doesn't already exist.
    '''
    print("accepted args")
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
def list_keywords_for_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids)).distinct()
        ds = session.exec(ds_cmd).one_or_none()
        ds_keywords = session.exec(select(Keyword).select_from(join(Keyword, DatasetKeywordLink, DatasetKeywordLink.keyword_id == Keyword.id)).where(DatasetKeywordLink.dataset_id == ds.id)).all()
        return ds_keywords


@app.post("/datasets/{dsid}/keywords", response_model = Keyword)
def add_keyword(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, keyword: str):
    '''
    Adds a keyword to a dataset. If the keyword exists, it will simply add the existing keyword to the dataset object. 
    Otherwise, the keyword will be created and then added to the dataset. 
    '''
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
def read_dataset_drive_location(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of DataDriveLocation objects with one object for each location in google drive where the dataset is currently located.
    '''
    with Session(engine) as session:
        ds_locs = session.exec(select(DataDriveLocation).where(and_(DataDriveLocation.dataset_id == dsid, DataDriveLocation.status == 'current'))).all()
    return(ds_locs)


@app.post("/datasets/{dsid}/drive_location", response_model = DataDriveLocation)
def create_dataset_drive_location(ag_ids:  Annotated[str, Depends(authorize_user)], dsid: str, drive_info: DataDriveLocation):   
    """
    Add a Google Drive location record to the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to 
        dsid: Dataset ID
        D: Google Drive information to be added as DataDriveLocation object 
        
    Returns:
        Added DataDriveLocation object

    """
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


@app.get("/datasets/{dsid}/access_groups", response_model = List[DatasetACL])
def read_dataset_acl(dsid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    with Session(engine) as session:
        ds_acl = session.exec(select(AccessGroup).select_from(join(AccessGroup, join(DatasetACL, 
                                                                                     Dataset, DatasetACL.dataset_id == Dataset.id),
                                                                                     AccessGroup.id == DatasetACL.accessgroup_id)).where(Dataset.unique_id == dsid)).all()
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
def create_dataset_collection(ag_ids:  Annotated[str, Depends(authorize_user)], dc: DatasetCollection):
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
def list_user_access_groups(ag_ids:  Annotated[str, Depends(authorize_user)], orcid: str):
    with Session(engine) as session:
        ags = session.exec(select(AccessGroup).where(AccessGroup.id).in_(ag_ids)).all()
        return([ag.group_name for ag in ags])



# ===============================================================================================================================================================

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
def list_instrument(ag_ids: Annotated[str, Depends(authorize_user)], request: Request):
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
def read_instrument(ag_ids: Annotated[str, Depends(authorize_user)], instrument_unique_id: str):
    '''
    Returns an instrument with the provided ID if one exists. Otherwise returns None. 
    '''
    with Session(engine) as session:
        inst = session.exec(select(Instrument).where(Instrument.unique_id == instrument_unique_id)).one_or_none()
        return(inst)


@app.post("/instruments", response_model = Instrument)
def create_instrument(ag_ids:  Annotated[str, Depends(authorize_user)], instrument: Instrument):   
    """
    Add a new instrument record to the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to; here only used to confirm user is in the database
        instrument: Instrument object to be added
        
    Returns:
        Added instrument object
    """
    instrument.instrument_name = instrument.instrument_name.lower()
    with Session(engine) as session:
        session.add(instrument)
        session.commit()
        session.refresh(instrument)
    return(instrument)


@app.get("/google_drives", response_model = List[GoogleDrives])
def list_google_drive_info(ag_ids:  Annotated[str, Depends(authorize_user)], request: Request):
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
def read_google_drive_info(ag_ids:  Annotated[str, Depends(authorize_user)], drive_name: str):
    """
    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        drive_id: Google Drive Name to query
        
    Returns:
        Google Drive object if found, otherwise None
    """
    with Session(engine) as session:
        drive = session.exec(select(GoogleDrives).where(GoogleDrives.drive_name == drive_name)).all()

    return(drive)


@app.post("/google_drives", response_model = GoogleDrives)
def create_google_drive_info(ag_ids:  Annotated[str, Depends(authorize_user)], D: Dict):
    """
    Add or update a Google Drive record in the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        D: Google Drive information to be added or updated
        
    Returns:
        Added or updated Google Drive object
    """
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

# ===============================================================================================================================================================
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
            
            apikey = get_secret("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/3")
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
def create_project(admin_acct:  Annotated[str, Depends(is_admin)], P: Project):
    """
    Add or update a project record in the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
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
def read_project(proj_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns the project object for the provided ID
    '''
    with Session(engine) as session:
        prop = session.exec(select(Project).where(Project.project_id == proj_id)).one_or_none()
        return(prop)


@app.get("/projects/{proj_id}/users", response_model = List[User])
def read_users_on_project(proj_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of users associated with the project ID provided
    '''
    with Session(engine) as session:
        users = session.exec(select(User).select_from(join(User, UserProjectLink, UserProjectLink.user_id == User.id)).where(UserProjectLink.project_id == proj_id)).all()
        return users
    


# if __name__ == "__main__":

#     # run locally with: python -m app.api.main --db_config cloud_config.yaml --api_config "conf/api_conf.yaml"
#     print("running as main")
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=7777)