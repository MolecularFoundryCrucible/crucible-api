from typing import Annotated, List, Tuple, Dict, Optional
import json
import yaml
import requests
from jose import jwt
import urllib.parse
import datetime
import pytz
import os

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from starlette.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer


from sqlmodel import Session, select
from sqlalchemy import join, func
from sqlalchemy.sql import and_

from app.general_utils import get_secret
from app.utils.sql_utils import setup_engine, add_or_update
from app.utils.rmq_utils import send_pika_message


from app.models.core_models import Dataset, DatasetACL, Keyword, DatasetKeywordLink, Thumbnail, AssociatedFile, User, AccessGroup, Project, UserAccessGroupLink, UserProjectLink, DatasetCollection, DatasetCollectionLink
from app.models.instrument import Instrument
from app.models.misc import IdType, DataDriveLocation, GoogleDrives
from app.models.tasks import TransferRequest, SciCatUpdateRequest, IngestionRequest

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

db_config_file = os.getenv("DB_CONF_FILE")
db_config_file = f"{home_dir}/{db_config_file}"
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
    from utils.auth_utils import SECRET_KEY, ALGORITHM, code_for_token_exchange
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
    from utils.auth_utils import decode_token, find_or_add_api_access
    user_info_jwt = request.cookies.get("crucible_user_token")
    
    if user_info_jwt is None:
        response = RedirectResponse("/api/login")
        response.set_cookie("after_login_return_to_page", value = "/api/user_apikey", httponly = True, secure = True)
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
    from utils.auth_utils import decode_token, find_or_add_api_access
    user_info_jwt = request.cookies.get("crucible_user_token")
    
    if user_info_jwt is None:
        response = RedirectResponse("/api/login")
        response.set_cookie("after_login_return_to_page", value = "/api/regenerate_apikey", httponly = True, secure = True)
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
                         dataset: Dataset, 
                         file: Optional[Annotated[UploadFile, File(description="A file read as UploadFile")]] = None):
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

    if file:
        # save the file to crucible-uploads
        save_file_path = f"/root/uploads/{file.filename}"
        with open(save_file_path, "wb") as f:
            f.write(await file.read())
        dataset.file_to_upload == file.filename


    apikey = get_secret("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/1")
    with Session(engine) as session:

        # add any existing information about the dataset to the SQL database
        ds = add_or_update(session, Dataset, Dataset.unique_id == dataset.unique_id, **dataset)
        session.commit()

        # set appropriate access controls
        ro = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": "readonly", "read": True, "write": False}, 
                            headers = {"Authorization":f"Bearer {apikey}"})
        
        admin = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": "admin", "read": True, "write": True}, 
                            headers = {"Authorization":f"Bearer {apikey}"})
        
        
        if dataset.owner_user_id is not None:
            user_orcid = session.exec(select(User.orcid).where(User.id == dataset.owner_user_id))
            user_ag = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": user_orcid, "read": True, "write": True}, 
                            headers = {"Authorization":f"Bearer {apikey}"})

        if dataset.project_id is not None:
            prop_name = session.exec(select(Project.project_name).where(Project.id == dataset.project_id))
            prop_ag = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": prop_name, "read": True, "write": False}, 
                            headers = {"Authorization":f"Bearer {apikey}"})

        if dataset.instrument_id is not None:
            inst_name = session.exec(select(Instrument.name).where(Instrument.id == dataset.instrument_id))
            inst_ag = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": inst_name, "read": True, "write": False}, 
                            headers = {"Authorization":f"Bearer {apikey}"})
            
        add_or_update(session, IdType, where=(IdType.unique_id == dataset.unique_id), unique_id = dataset.unique_id, type = "dataset")
        session.commit()


    if dataset.file_to_upload is not None:
        # call ingest endpoint 
        apikey = get_secret("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/1")
        payload  = {"dataset": dataset}

        ds = requests.post(url = f"{api_url}/datasets/{dataset.unique_id}/ingest",  
                            headers = {"Authorization":f"Bearer {apikey}"}, 
                            data = payload   #TODO: check this request format
                          )
    
    return ds


@app.post("/datasets/{dsid}/ingest", response_model = IngestionRequest)
def request_dataset_ingestion(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str,  dataset: Dataset):
    '''
    This sends a rabbitMQ message to the ingestion queue.
    The current ingestion queue is only expecting a "file_to_upload" value which this has enough information to provide. 

    TODO: In order to associate the ingested data back to the dsid provided:
      - the dsid needs to be sent with the message
      - rmq consumer needs to be updated to expect a dsid
      - ingestion class methods need to use the dsid provided ...
      - Need to consider how the system works when a dsid exists INSIDE the file

    Done - To follow the setup of the other "request functions" this should result in a record being created in the "IngestionRequest" table. 


    '''
    pst= pytz.timezone("America/Los_Angeles")
    creation_time = datetime.datetime.now(pst).isoformat()
    ingestion_request = IngestionRequest({"dataset_id": dsid, "time_created": creation_time, "file_to_upload": dataset.file_to_upload})
    with Session(engine) as session:
        session.add(ingestion_request)
        session.commit()

    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
    send_pika_message(message = dataset.file_to_upload, exchange= "", routing_key= "ingest2", user = 'admin', rmq_pw= rmq_pw)

    return ingestion_request


@app.post("/datasets/{dsid}/transfer", response_model = TransferRequest)
def request_dataset_transfer(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, source_path: str, destination_path: str):
    '''
    This concept is fairly different from what we currently have ..

    Here you could put the logic from the CrucibleDatasetTransfer Object in pycrucible into an actual python client. 
    Then the client supplies the transfer info to the API. 
    This endpoint uploads the dsid and destination info to a database record. 
    Additionally it will send the RMQ message with bindings for the transfer queue
    The downstream consumer then has the majority of the info it needs and can do the transfer (maybe without reading the json)

    **you could also replace source/destination "paths" with some sort of schema that tells what type of path this is / what type of program should interpret it? 
    Will it always be rclone? I guess so..... 

    Also worth noting --- the endpoint will need to be called multiple times per dataset (x4 prob)
    User Drive, Prop Drive x Organized, Flat

    **** google drive transfer with semi hardcoded paths
    '''

    with Session(engine) as session:
        pst= pytz.timezone("America/Los_Angeles")
        creation_time = datetime.datetime.now(pst).isoformat()

        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    
        with Session(engine) as session:
            ds_list = session.exec(ds_cmd).all()
    
        if ds_list is None:
            raise HTTPException(status_code=402, detail="Dataset not found")
        elif len(ds_list) == 0:
            raise HTTPException(status_code=402, detail="Dataset not found")
        else:
            pass

        transfer_request = TransferRequest({"dataset_id": dsid, "time_created": creation_time, "source_path": source_path, "destination_path": destination_path})
        session.add(transfer_request)
        session.commit()

        # TODO: probably want to update this on the rabbit mq side .. 
        # 1) to match the request and be more general dsid, source, dest and 
        # 2) exchange name is potentially limiting / inaccurate in new set up?
        # kind of brings into question the design choice of having a topic exchange since the WHOLE point of this is to allow multiple points of entry...
        # and the whole point of that was to send one message from ingestion to all the services that should happen if data is ingested.. (not very modular)
        # maybe thats not true bc you can still send the other message with full bindings to the same exchange and this one just has the partial binding to do this one thing..
        # see other notes in comment above
        message = json.dumps({"dsid":dsid, "source_bucket":"mf-storage-prod"})
        rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
        
        # TODO: this will need to live somewhere - see if there is a way to check exchange / channel / binding existence and establish if not ///// then can live in rmq_utils OR crucible-rabbit-mq but make sure that you don't forget to make it!
        # channel.exchange_declare(exchange='raw-data-processing', exchange_type='topic')
        # transfer_queue = channel.queue_declare(queue='q-transfer-to-drive')
        # channel.queue_bind(exchange='raw-data-processing', queue=transfer_queue.method.queue, routing_key = '#.transfer-request.#')

        send_pika_message(message, exchange = "raw-data-processing", routing_key= "#.transfer-request.#", user = 'admin', rmq_pw= rmq_pw)

        # send rabbitMQ message to transfer
        #message = json.dumps({"dsid": dsid, "source_bucket": source_path, "destination_path": destination_path, "request_id": transfer_request.id})
        #connect_and_publish(message, "ingested-data", "transfer-request")


        return(transfer_request)


@app.post("datasets/{dsid}/scicat_update", response_model = SciCatUpdateRequest)
def request_update_scicat_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    # this endpoint would send rmq message that would call scicat API 
    # does it even make sense?
    # consumer would do more than that but still...
    # add for now though for gradual compatibilty
    pst= pytz.timezone("America/Los_Angeles")
    creation_time = datetime.datetime.now(pst).isoformat()

    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))

    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=402, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=402, detail="Dataset not found")
    else:
        pass

    scicat_request = SciCatUpdateRequest({"dataset_id": dsid, "time_created": creation_time})
    session.add(scicat_request)
    session.commit()

    # TODO: confirm rabbit mq side matches // could make new vs. update field in message?
    message = json.dumps({"dsid": dsid, "source_bucket":"mf-storage-prod"})
    rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
    
    # TODO: this will need to live somewhere - see if there is a way to check exchange / channel / binding existence and establish if not ///// then can live in rmq_utils OR crucible-rabbit-mq but make sure that you don't forget to make it!
    # channel.exchange_declare(exchange='raw-data-processing', exchange_type='topic')
    # transfer_queue = channel.queue_declare(queue='q-transfer-to-drive')
    # channel.queue_bind(exchange='raw-data-processing', queue=transfer_queue.method.queue, routing_key = '#.transfer-request.#')

    send_pika_message(message, exchange = "raw-data-processing", routing_key= "#.scicat-request.#", user = 'admin', rmq_pw= rmq_pw)


@app.patch("/datasets/{dsid}", response_model = Dataset)
def update_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, updated_dataset: Dataset):
    '''
    Updates a dataset in SQL and returns the Updated Dataset object. 
    Sends a message to RabbitMQ to alert RMQ that updates have been made.
    '''

    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=402, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=402, detail="Dataset not found")
    else:
        pass
    
    apikey = get_secret("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/1")

    with Session(engine) as session:
        ds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **updated_dataset)
        if updated_dataset.owner_user_id is not None:
            user_orcid = session.exec(select(User.orcid).where(User.id == updated_dataset.owner_user_id))
            user_ag = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": user_orcid, "read": True, "write": True}, 
                            headers = {"Authorization":f"Bearer {apikey}"})

        if updated_dataset.project_id is not None:
            prop_name = session.exec(select(Project.project_name).where(Project.id == updated_dataset.project_id))
            prop_ag = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": prop_name, "read": True, "write": False}, 
                            headers = {"Authorization":f"Bearer {apikey}"})

        if updated_dataset.instrument_id is not None:
            inst_name = session.exec(select(Instrument.name).where(Instrument.id == updated_dataset.instrument_id))
            inst_ag = requests.request("post",
                            url =  f"{api_url}/datasets/{ds.id}/access_groups", 
                            params = {"group_name": inst_name, "read": True, "write": False}, 
                            headers = {"Authorization":f"Bearer {apikey}"})
            

        message = json.dumps(updated_dataset)
        rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
    
        # TODO: check how dsupdate and dsupdate-owner consumers differ / what are the inputs expected --- can we consolidate?
        # note: dsupdate-owner is expected the message to contain the field updated_ownership_info whereas dsupdate is expected updated_info
        #       both are expecting "dataset_to_update": pid
        #       update_info can be any dictionary
        #       updated_ownership_info is an instance of the Owner schema
        # Really this will have to change to work but the current format would look like: 
        # message = json.dumps({"dataset_to_update": updated_dataset.unique_id, "updated_info": dict(updated_dataset)})
        # and for ownership it would look like: 
        # message = json.dumps({"dataset_to_update": updated_dataset.unique_id, "updated_ownership_info": dict(Owner(subset of updated_dataset))})
        send_pika_message(message, exchange = "", routing_key= "dsupdate", user = 'admin', rmq_pw= rmq_pw)

        return ds


@app.get("/datasets", response_model = List[Dataset])
def list_datasets(ag_ids: Annotated[str, Depends(authorize_user)], request = Request):
    '''
    Returns a list of all dataset IDs the requesting user is authorized to view. 
    If no dataset IDs are authorized for the user, an empty list will be returned. 

    '''
    query_params = request.query_params
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
    
    
@app.get("/datasets/{dsid}", response_model = Dataset)
def read_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a dataset found with the provided unique ID if one is exists and the requesting user is authorized to view the dataset. 
    Otherwise, returns None
    '''
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        return ds
    

@app.delete("/datasets/{dsid}", response_model = Dataset)
def delete_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid = str):
    # TODO: DEFINE THIS
    pass


@app.get("/datasets/{dsid}/thumbnails", response_model = List[Thumbnail])
def read_thumbnails(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of thumbnail objects associated with the dataset ID provided
    '''
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        ds_thumbnails = session.exec(select(Thumbnail).where(Thumbnail.dataset_id == ds.id)).all()
        return ds_thumbnails


@app.post("/datasets/{dsid}/thumbnails", response_model = Thumbnail)
def add_thumbnail(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, thumbnail: str):
    '''
    Adds a thumbnail to a dataset. If the thumbnail exists, it will simply add the existing thumbnail to the dataset object. 
    Otherwise, the thumbnail will be created and then added to the dataset. 
    '''
    with Session(engine) as session:
        tn_obj = add_or_update(session, Thumbnail, where=(Thumbnail.thumbnail_b64str == thumbnail['thumbnail']),
                        thumbnail_name = thumbnail['caption'], 
                        thumbnail_b64str = thumbnail['thumbnail'])
        
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    
    with Session(engine) as session:
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        if not (tn_obj in ds.thumbnails):  
            ds.thumbnails.append(tn_obj)
        session.add(ds)
        session.commit()
        return(tn_obj)


@app.get("/datasets/{dsid}/associated_files", response_model = List[AssociatedFile])
def read_associated_files(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of the files associated with the dataset ID provided. 
    '''
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        ds_files = session.exec(select(AssociatedFile).where(AssociatedFile.dataset_id == ds.id)).all()
        return ds_files



@app.post("/datasets/{dsid}/associated_files", response_model = AssociatedFile)
def add_associated_file(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, associated_file: AssociatedFile):
    '''
    Adds an associated file to a dataset. 
    Creates the AF object if it doesn't already exist.
    '''
    with Session(engine) as session:
        af_obj = add_or_update(session, AssociatedFile, where=(AssociatedFile.filename == associated_file.filename),
                                       **associated_file)

        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        if not (af_obj in ds.associated_files):  
            ds.associated_files.append(af_obj)

        session.add(ds)
        session.commit()
        return(af_obj)
 

@app.get("/datasets/{dsid}/keywords", response_model = List[Keyword])
def list_keywords_for_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        ds_keywords = session.exec(select(Keyword).select_from(join(DatasetKeywordLink, DatasetKeywordLink.keyword_id == Keyword.id)).where(DatasetKeywordLink.dataset_id == ds.id)).all()
        return ds_keywords


@app.post("/datasets/{dsid}/keywords", response_model = Keyword)
def add_keyword(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, keyword: str):
    '''
    Adds a keyword to a dataset. If the keyword exists, it will simply add the existing keyword to the dataset object. 
    Otherwise, the keyword will be created and then added to the dataset. 
    '''
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    
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
        return(kw_obj)


@app.get("/datasets/{dsid}/drive_location", response_model = DataDriveLocation)
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
    ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()

    if ds_list is None:
        raise HTTPException(status_code=402, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=402, detail="Dataset not found")
    else:
        add_or_update(session, 
                      DataDriveLocation,
                      DataDriveLocation.folder_id == drive_info.folder_id,
                      dataset_id = dsid, 
                      **drive_info)
        session.commit()

    return(drive_info)


@app.get("/datasets/{dsid}/access_groups", response_model = DatasetACL)
def read_dataset_acl(dsid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    with Session(engine) as session:
        ds_acl = session.exec(select(AccessGroup).select_from(join(AccessGroup, join(DatasetACL, 
                                                                                     Dataset, DatasetACL.dataset_id == Dataset.id),
                                                                                     AccessGroup.id == DatasetACL.accessgroup_id)).where(Dataset.unique_id == dsid)).all()
        return ds_acl


@app.post("/datasets/{dsid}/access_groups", response_model = DatasetACL)
def create_dataset_acl(group_name: str, dsid: str, read: str, write: str, admin_acct: Annotated[str, Depends(is_admin)]):
    with Session(engine) as session:
        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()
        ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()

        acl = session.exec(select(DatasetACL).where(DatasetACL.accessgroup == ag).where(DatasetACL.dataset == ds)).one_or_none()
    if not acl:
        acl = DatasetACL(accessgroup=ag, dataset=ds)
        acl.read = read
        acl.write = write
        session.add(acl)
        session.commit()

    return(acl)


@app.post("/dataset_collections", response_model = DatasetCollection)
def create_dataset_collection(ag_ids:  Annotated[str, Depends(authorize_user)], dc: DatasetCollection):
    with Session(engine) as session:
        session.add(dc)
        session.commit()
    return(dc)


@app.get("/dataset_collections/{dcid}", response_model = DatasetCollection)
def read_dataset_collection(admin_acct: Annotated[str, Depends(is_admin)], dcid: str):
    with Session(engine) as session:
        dc = session.exec(select(DatasetCollection).where(DatasetCollection.unique_id == dcid)).one_or_none()
    return(dc)


@app.get("/dataset_collections", response_model = List[DatasetCollection])
def list_dataset_collections(admin_acct: Annotated[str, Depends(is_admin)], request = Request):
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


@app.get("/access_groups/{group_name}", response_model = AccessGroup)
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
        
    return(ag)



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
def list_instrument(ag_ids: Annotated[str, Depends(authorize_user)], request = Request):
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

    for k,v, in params_dict.items():
        if hasattr(Instrument, k):
            statement = statement.where(getattr(Instrument, k) == v.lower().strip())
    with Session(engine) as session: 
        all_inst = session.exec(statement).all()

        return(all_inst)


@app.get("instruments/{instrument_unique_id}", response_model = Instrument)
def read_instrument(admin_acct: Annotated[str, Depends(is_admin)], instrument_unique_id: str):
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
    with Session(engine) as session:
        session.add(instrument)
        session.commit()
    
    return(instrument)


@app.get("/google_drives", response_model = List[GoogleDrives])
def list_google_drive_info(ag_ids:  Annotated[str, Depends(authorize_user)], request = Request):
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


@app.get("/google_drives/{drive_id}", response_model = GoogleDrives)
def read_google_drive_info(ag_ids:  Annotated[str, Depends(authorize_user)], drive_id: str):
    """
    Add or update a Google Drive record in the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        drive_id: Google Drive ID to query
        
    Returns:
        Google Drive object if found, otherwise None
    """
    with Session(engine) as session:
        drive = session.exec(select(GoogleDrives).where(GoogleDrives.drive_id == drive_id)).one_or_none()

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


@app.get("/users/{orcid}", response_model = User)
def read_user(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns the User object associated with the provided ORCID if found. Otherwise returns None. 
    '''
    with Session(engine) as session:
        users = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        return users


@app.post("/users", response_model = User)
def create_user(admin_acct:  Annotated[str, Depends(is_admin)], user_info: User, project_names: List):  
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
            user = add_or_update(session, User, User.orcid == user_info.orcid, **user_info)
            session.commit()
        
            apikey = get_secret("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/1")
            requests.request("post", url =  f"{api_url}/users/{user.orcid}/access_groups/{user.orcid}", headers = {"Authorization":f"Bearer {apikey}"})
        
            # for each project that the user is a USER on: 
            # 1. get the project object from the database
            # 2. add the project to the user object
            # 3. add an access group for the project, named by the project and add the user to the access group
            for pname in project_names:
                props = session.exec(select(Project).where(Project.project_name == pname)).all()
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


@app.get("/users/{orcid}/datasets", response_model = List[Dataset])
def read_user_datasets(orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of all the datasets the user has access to based on the ORCID provided. 
    '''
    with Session(engine) as session:
        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_list =session.exec(select(Dataset).select_from(join(DatasetACL, Dataset, DatasetACL.dataset_id == Dataset.id)).where(DatasetACL.accessgroup_id.in_(ag_ids))).all()
        return ds_list


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
        if len(ds_acl) > 0:
            return {"read": ds_acl.read, "write": ds_acl.write}
        else:
            return {"read": False, "write": False}


@app.get("/users/{orcid}/projects", response_model = List[Project])
def read_projects_for_user(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns a list of the projects that this User is associated with. 
    '''
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        user_id = user.id
        projects = session.exec(select(Project).select_from(join(Project, UserProjectLink, Project.id == UserProjectLink.project_id)).where(UserProjectLink.user_id == user_id)).all()
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
        prop = add_or_update(session, Project,  Project.id == P.id, **P)
        ag = add_or_update(session, 
                      AccessGroup, 
                      AccessGroup.group_name==P.name, 
                      group_name=P.name)
        
        r1 = session.exec(select(User).where(User.lbl_email.ilike(f"%{P.project_lead_email}%"))).one_or_none()
        r2 = session.exec(select(User).where(User.email.ilike(f"%{P.project_lead_email}%"))).one_or_none()
        all_found_accounts = list(set([x for x in r1 + r2 if x['orcid'] is not None]))
        
        print(f"found accounts for user: {all_found_accounts}")

        if len(all_found_accounts) > 0:
            project_lead = all_found_accounts[-1]
            if not (project_lead in ag.users):
                ag.users.append(project_lead)

            if not (prop in project_lead.projects):
                project_lead.append(prop)

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


@app.get("/projects/{proj_id}", response_model = Project)
def read_project(proj_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    '''
    Returns the project object for the provided ID
    '''
    with Session(engine) as session:
        prop = session.exec(select(Project).where(Project.id == proj_id)).one_or_none()
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