from typing import Annotated, List, Tuple, Dict, Any, Optional
import json
import requests
import subprocess as sp
from jose import jwt, JWTError
import urllib.parse
import datetime
import pytz

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPBearer
from pycrucible.general_functions import  get_secret_2


from sqlmodel import Session, select
from sqlalchemy import join, func
from sqlalchemy.sql import and_

from utils.auth_utils import authorize_user, is_admin
from utils.sql_utils import setup_engine, add_or_update
from app.schemas.schema import EmptyDataset
from app.models.dataset import Dataset, DatasetACL, Keyword, DatasetKeywordLink, Thumbnail, AssociatedFile
from app.models.instrument import Instrument
from app.models.user import User, AccessGroup, Proposal
from app.models.misc import IdType, DataDriveLocation
from app.models.tasks import TransferRequest
from app.utils.rmq_utils import send_pika_message

# ======================= APP
app = FastAPI()
security = HTTPBearer()


# ====== Oauth config
hostname = "crucible.lbl.gov"
orcid_client_id = "APP-AXDIPEVR6IMA0RL7"
orcid_client_secret = get_secret_2("CLIENT_SECRET", "projects/776258882599/secrets/orcid_client_secret/versions/2")
redirect_uri = urllib.parse.quote_plus("https://crucible.lbl.gov/api/auth")


# ====== Database config
connect_args = {"check_same_thread": False}
engine = setup_engine("cloud_config.yaml")


# where to put this?
def update_acl(group_name, ds, session, read, write):
    ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == group_name)).one_or_none()
    curr_acl = session.exec(select(DatasetACL).where(DatasetACL.accessgroup == ag).where(DatasetACL.dataset == ds)).one_or_none()
    if not curr_acl:
        acl = DatasetACL(accessgroup=ag, dataset=ds)
        acl.read = read
        acl.write = write
        session.add(acl)


# ======================= AUTH AND ACCOUNT SET UP ENDPOINTS
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
    token = code_for_token_exchange(code, orcid_client_id, orcid_client_secret, "https://crucible.lbl.gov/api/auth")                                
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
@app.get("/datasets")
def list_datasets(ag_ids: Annotated[str, Depends(authorize_user)], request = Request):
    '''
    Returns a list of all dataset IDs the requesting user is authorized to view. 
    If no dataset IDs are authorized for the user, an empty list will be returned. 

    '''
    query_params = request.query_params
    params_dict = dict(query_params)

    ds_cmd = select(Dataset.unique_id).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).filter(DatasetACL.accessgroup_id.in_(ag_ids))
    
    for k, v in params_dict.items():
        if hasattr(Dataset, k):
            ds_cmd = ds_cmd.where(getattr(Dataset, k) == v)
        elif k == "keyword":
            # TODO: move this logic to before the initial ds_cmd so that you can add other where queries to it ... or maybe keyword needs to be an official parameter / data field?
            K = session.exec(select(Keyword).where(Keyword.keyword.ilike(f"%{v}%"))).all()
            if K:
                k_ids = [k.id for k in K]
                ds_cmd = select(Dataset).select_from(join(DatasetKeywordLink,
                                                join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id),
                                                     DatasetKeywordLink.dataset_id == Dataset.id))\
                                                .filter(DatasetACL.accessgroup_id.in_(ag_ids))\
                                                .filter(DatasetKeywordLink.keyword_id.in_(k_ids))
        else: 
            pass

        # TODO: would also be good to add logic to: 
        #       get user_id from orcid / email
        #       get proposal_id from proposal name
        #       filter on creation time range? 
        #       contains a sample ID?
        #       includes an associated file name?
        # mainly the first 2

    with Session(engine) as session:
        ds_cmd = ds_cmd # I think the filter has to occur after the "where"
        ds_list = session.exec(ds_cmd).all()
        return ds_list
    
    
@app.get("/datasets/{dsid}")
def read_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a dataset found with the provided unique ID if one is exists and the requesting user is authorized to view the dataset. 
    Otherwise, returns None
    '''
    with Session(engine) as session:
        ds_cmd = select(Dataset).select_from(join(Dataset, DatasetACL, DatasetACL.dataset_id == Dataset.id)).where(Dataset.unique_id == dsid).filter(DatasetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        return ds
    



@app.get("/datasets/{dsid}/drive_location")
def read_dataset_drive_location(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    Returns a list of DataDriveLocation objects with one object for each location in google drive where the dataset is currently located.
    '''
    with Session(engine) as session:
        ds_locs = session.exec(select(DataDriveLocation).where(and_(DataDriveLocation.dataset_id == pid, DataDriveLocation.status == 'current'))).all()
    return(ds_locs)


@app.post("/datasets")
def create_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dataset: EmptyDataset):
    '''
    Creates a dataset in SQL and returns the Dataset object that was created. 
    Creating the dataset in SQL will result in a message being passed to RabbitMQ to alert RMQ that a new dataset has been added.
    Data and metadata can be added to a dataset at the time of or after the record creation.
    By default it will give access to the admin and readonly accounts. If user information, proposal, or instrument are included, 
    then access will also be given to the relevant groups. 
    The dataset ID is recorded in a table to track that the mfid corresponds to a dataset. 
    '''

    # TODO: This needs to actually handle a couple use cases: 
    # 1) A user wants to create a dataset record before they have any data (streaming use case) (current endpoint here)
    # 2) A user wants to create a dataset record from a raw file (ingest data) (dataset_upload endpoint)
    # 3) A user wants to create a datatset record directly from json (current endpoint here / variation on upload_with_metadata)

    with Session(engine) as session:

        # add any existing information about the dataset to the SQL database
        ds = add_or_update(session, Dataset, Dataset.unique_id == dataset.unique_id, **dataset)

        # set appropriate access controls
        update_acl('readonly', ds, session, read = True, write = False)
        update_acl('admin', ds, session, read = True, write = True)

        if dataset.owner_user_id is not None:
            user_orcid = session.exec(select(User.orcid).where(User.id == dataset.owner_user_id))
            update_acl(user_orcid, ds, session, read = True, write = True)

        if dataset.proposal_id is not None:
            prop_name = session.exec(select(Proposal.name).where(Proposal.id == dataset.proposal_id))
            update_acl(prop_name, ds, session, read = True, write = False)

        if dataset.instrument_id is not None:
            inst_name = session.exec(select(Instrument.name).where(Instrument.id == dataset.instrument_id))
            update_acl(inst_name, ds, session, read = True, write = False)
        
        add_or_update(session, IdType, where=(IdType.unique_id == dataset.unique_id), unique_id = dataset.unique_id, type = "dataset")
        session.commit()
        # kick off ingestion for file to upload
        if dataset.file_to_upload is not None:
            rmq_pw = get_secret_2("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
            # this message is going to start a process that will act on the file_to_upload 
            # to extract other fields and create a json representation of the dataset
            # currently that ingestion process would also then add or update the existing dataset record in the sql database 
            # using the created json file
            send_pika_message(message = dataset.file_to_upload, exchange= "", routing_key= "ingest2", user = 'admin', rmq_pw= rmq_pw)

        return ds


@app.post("/datasets/{dsid}/transfer")
def transfer_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, source_path: str, destination_path: str):
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
        rmq_pw = get_secret_2("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
        
        # TODO: this will need to live somewhere - see if there is a way to check exchange / channel / binding existence and establish if not ///// then can live in rmq_utils OR crucible-rabbit-mq but make sure that you don't forget to make it!
        # channel.exchange_declare(exchange='raw-data-processing', exchange_type='topic')
        # transfer_queue = channel.queue_declare(queue='q-transfer-to-drive')
        # channel.queue_bind(exchange='raw-data-processing', queue=transfer_queue.method.queue, routing_key = '#.transfer-request.#')

        send_pika_message(message, exchange = "raw-data-processing", routing_key= "#.transfer-request.#", user = 'admin', rmq_pw= rmq_pw)

        # send rabbitMQ message to transfer
        #message = json.dumps({"dsid": dsid, "source_bucket": source_path, "destination_path": destination_path, "request_id": transfer_request.id})
        #connect_and_publish(message, "ingested-data", "transfer-request")


        return(transfer_request)


@app.patch("/datasets/{dsid}")
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
    
    with Session(engine) as session:
        ds = add_or_update(session, Dataset, Dataset.unique_id == dsid, **updated_dataset)
        if updated_dataset.owner_user_id is not None:
            user_orcid = session.exec(select(User.orcid).where(User.id == updated_dataset.owner_user_id))
            update_acl(user_orcid, ds, session, read = True, write = True)

        if updated_dataset.proposal_id is not None:
            prop_name = session.exec(select(Proposal.name).where(Proposal.id == updated_dataset.proposal_id))
            update_acl(prop_name, ds, session, read = True, write = False)

        if updated_dataset.instrument_id is not None:
            inst_name = session.exec(select(Instrument.name).where(Instrument.id == updated_dataset.instrument_id))
            update_acl(inst_name, ds, session, read = True, write = False)


        message = json.dumps(updated_dataset)
        rmq_pw = get_secret_2("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
    
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


# delete?
@app.delete("/datasets/{dsid}")
def delete_dataset(ag_ids: Annotated[str, Depends(authorize_user)], dsid = str):
    pass





@app.get("/datasets/{dsid}/thumbnails")
def read_thumbnails(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    
    '''
    # TODO: define this 
    pass



@app.post("/datasets/{dsid}/thumbnails")
def add_thumbnail(ag_ids: Annotated[str, Depends(authorize_user)], thumbnail: str):
    '''
    Adds a thumbnail to a dataset. If the thumbnail exists, it will simply add the existing thumbnail to the dataset object. 
    Otherwise, the thumbnail will be created and then added to the dataset. 
    '''
    with Session(engine) as session:
        tn_obj = add_or_update(session, Thumbnail, where=(Thumbnail.thumbnail_b64str == thumbnail['thumbnail']),
                        thumbnail_name = thumbnail['caption'], 
                        thumbnail_b64str = thumbnail['thumbnail'])
        

        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        if not (tn_obj in ds.thumbnails):  
            ds.thumbnails.append(tn_obj)
        session.add(ds)
        session.commit()
        return(tn_obj)



@app.get("/datasets/{dsid}/associated_files")
def read_associated_files(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str):
    '''
    
    '''
    # TODO: define this 
    pass



@app.post("/datasets/{dsid}/associated_files")
def add_associated_file(ag_ids: Annotated[str, Depends(authorize_user)],  associated_file: AssociatedFile):
    '''
    Adds an associated file to a dataset. 
    Creates the AF object if it doesn't already exist.
    '''
    with Session(engine) as session:
        af_obj = add_or_update(session, AssociatedFile, where=(AssociatedFile.filename == associated_file.filename),
                                       **associated_file)


        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()

        if ds is None:
            raise HTTPException(status_code=404, detail="Dataset not found")
        
        if not (af_obj in ds.associated_files):  
            ds.associated_files.append(af_obj)

        session.add(ds)
        session.commit()
        return(af_obj)
 
 # I think change these to /datasets/{dsid}/keywords

@app.get("/keywords/{keyword}")
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


@app.post("/keywords")
def add_keyword(ag_ids: Annotated[str, Depends(authorize_user)], dsid: str, keyword: str):
    '''
    Adds a keyword to a dataset. If the keyword exists, it will simply add the existing keyword to the dataset object. 
    Otherwise, the keyword will be created and then added to the dataset. 
    '''
    with Session(engine) as session:
        kw_obj = add_or_update(session, Keyword, where=(Keyword.keyword == keyword), keyword=keyword)

        ds = session.exec(select(Dataset).where(Dataset.unique_id == dsid)).one_or_none()

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




@app.post("/instrument", response_model = Instrument)
def add_instrument(ag_ids:  Annotated[str, Depends(authorize_user)], instrument: Instrument):   
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