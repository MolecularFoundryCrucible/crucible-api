from typing import Annotated, List, Tuple, Dict, Any, Optional
import json
import requests
import subprocess as sp
from jose import jwt, JWTError
import urllib.parse
import datetime

from pydantic import BaseModel
from fastapi import Depends, FastAPI, HTTPException, status, Request,File, UploadFile, Form
from starlette.requests import Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from auth_utils import *
from models import *
from pycrucible.general_functions import  get_secret_2


from sqlmodel import Session, select
from sqlalchemy.orm import  defer
from sqlalchemy.sql import and_
from sqlalchemy import select, join



# =================  CREATE NEW RECORD ENDPOINTS
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


@app.post("/user")
def add_or_update_user(ag_ids:  Annotated[str, Depends(authorize_user)], user_info: Dict):  
    """
    Add or update a user record in the database.
    Add the user to an Access Group with their ORCID
    Add the user to Access Groups for any proposal they are a USER on // DOES NOT ADD Users to access groups for the proposals on which they are the assigned scientist
    Create a link to the proposals that the User is a USER on

    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        user_info: User information to be added or updated
        
    Returns:
        Added or updated user object
    """
    props = user_info.pop("proposals")
    
    with Session(engine) as session:
        if True:
            # add user to user table
            user = add_or_update(session, User, User.orcid == user_info['orcid'], **user_info)
            session.commit()
        
            # add an access group named by the users orcid, containing the orcid as a user
            ag = add_or_update(session, AccessGroup, AccessGroup.group_name == user_info['orcid'], group_name = user_info['orcid'], users = [user])
        
            # for each proposal that the user is a USER on: 
            # 1. get the proposal object from the database
            # 2. add the proposal to the user object
            # 3. add an access group for the proposal, named by the proposal and add the user to the access group
            for pname in props:
                propid = pname
                prop = session.get(Proposal, propid)
                
                # some proposals don't exist!
                if prop:
                    # add proposal to user
                    if prop not in user.proposals:
                        user.proposals.append(prop)
                    
                    # add user to access group
                    ag = add_or_update(session, AccessGroup, AccessGroup.group_name==pname, group_name=pname)
                    if not (user in ag.users):
                        ag.users.append(user)
        
            session.add(user)
            session.commit()

    return(user_info)


@app.post("/proposal")
def add_or_update_proposal(ag_ids:  Annotated[str, Depends(authorize_user)], P: Dict):
    """
    Add or update a proposal record in the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        P: Proposal object to be added or updated
        
    Returns:
        Added or updated proposal object
    """
    from sqlmodel import select
    with Session(engine) as session:
        prop = add_or_update(session, Proposal,  Proposal.id == P['id'], **P)
                        # id = P['id'],
                        # ser_num = P['proposal_serial_number'],
                        # status= P['status'],
                        # title= P['title'],
                        # assigned_scientist_name = f"{P['assigned_scientist_first_name']} {P['assigned_scientist_last_name']}",
                        # assigned_scientist_email = P['assigned_scientist_email']
                        # )
        ag = add_or_update(session, 
                      AccessGroup, 
                      AccessGroup.group_name==P['id'], 
                      group_name=P['id'])
        
        r1 = session.exec(select(User).where(User.lbl_email.ilike(f"%{email}%"))).one_or_none()
        r2 = session.exec(select(User).where(User.email.ilike(f"%{email}%"))).one_or_none()
        all_found_accounts = list(set([x for x in r1 + r2 if x['orcid'] is not None]))
        
        print(f"found accounts for user: {all_found_accounts}")

        if len(all_found_accounts) > 0:
            assigned_scientist = all_found_accounts[-1]
            if not (assigned_scientist in ag.users):
                ag.users.append(assigned_scientist)

            if not (prop in assigned_scientist.proposals):
                assigned_scientist.append(prop)

        session.commit()

    return(P)




@app.post("/google_drive")
def add_or_update_google_drive_info(ag_ids:  Annotated[str, Depends(authorize_user)], D: Dict):
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
                                   # drive_id = D['drive_id'],
                                   # drive_name=D['drive_name'])
                
                session.commit()
                    
            except Exception as err:
                print(err)

    return(D)


@app.post("/dataset_drive_location/{dsid}")
def drive_location_to_cloud_sql(ag_ids:  Annotated[str, Depends(authorize_user)], dsid, D: Dict):   
    """
    Add a Google Drive location record to the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to ** should update to is_admin
        dsid: Dataset ID
        D: Google Drive information to be added
        
    Returns:
        Added Google Drive location object

    call this from cloud-data-triggers in response to new .transferred file
    open json, loop through google drive info field to add each
    """
    with Session(engine) as session:
        if True:
            ddl_ =  add_or_update(session, 
                                  DataDriveLocation,
                                  DataDriveLocation.folder_id == D['folder_id'],
                                  drive_name = D['drive_name'],
                                  dataset_id = dsid,
                                  folder_path_in_drive = D['folder_path_in_drive'],
                                  folder_id = D['folder_id'],
                                  url = D['url'],
                                  folder_last_mod = D['folder_last_mod'],
                                  status = D['status']
                            )
    
            session.commit()
    return(D)



@app.post("/sql_dataset_build/")
def build_dataset_in_sql_tables(ag_ids:  Annotated[str, Depends(authorize_user)], dsid: Optional[str] = None, D: Optional[Dict] = None):
    """
    Build a dataset record in the database.
    
    Args:
        ag_ids: List of access group IDs the user belongs to; only used to confirm user is in the database
        dsid: Dataset ID
        D: Dataset information to be added
        
    Returns:
        Added dataset object
    """
    from sqlmodel import select
    if D is None and dsid is not None:
        # get the data
        # read into D
        pass
    elif D is None:
        return("Please provide a dataset object or identifier")
    
    else:
        D = D

    
    uid = get_any(D, ['unique_id', 'dataset_unique_id' ])
    smd = get_any(D, ['scientific_metadata', 'metadata_dictionary'])
    owner_orcid= get_any(D, ['orcid'])
    measurement = get_any(D, ['measurement'])
    raw_or_derived = get_any(D, ['raw_or_derived'])
    

    # raw or derived
    if raw_or_derived is None:
        raw_or_derived = "raw"
    
    if raw_or_derived == "derived":
        file_to_upload = ""
        data_format = ""
    else:
        file_to_upload = D['file_to_upload']
        data_format = D['data_format']

    # public yes/no
    if "public" in D.keys():
        public_status = D['public']
    else:
        public_status = False
    
    #  ============================= get some info    
    baseurl = "https://crucible.lbl.gov/api"
    apikey = get_secret_2("ADMIN_APIKEY", "projects/776258882599/secrets/crucible_admin_apikey/versions/1")
    
    user = requests.request(method="get", url=f"{baseurl}/users/{owner_orcid}", headers = {"Authorization":f"Bearer {apikey}"}).json()
    print(f"{user=}")
    if user is not None:
        user_id = user['id']
        print(user, user_id)
    else:
        user_id = None

    # this needs to be fixed but it will break things right now ---- but the proposal endpoint expects proposal/MFP{XXXX} so "unknown" errors
    try:
        prop = requests.request(method="get", url=f"{baseurl}/proposal/{D['proposal']}", headers = {"Authorization":f"Bearer {apikey}"}).json()
        print(f"{prop=}")
        prop_id = prop['id']
        print(prop, prop_id)
    except:
        prop = None
        prop_id = None

    
    inst = requests.request(method="get", url=f"{baseurl}/instrument_by_name/{D['instrument_name']}", headers = {"Authorization":f"Bearer {apikey}"}).json()
    if inst is not None:
        inst_id = inst['identifier']
    else:
        inst_id = None

    with Session(engine) as session:

        # ============================= DATASET
        ds = add_or_update(session, DataSet, DataSet.unique_id == uid, 
                            unique_id=uid,
                            dataset_name=D['dataset_name'],
                            scientific_metadata=smd,
                            owner_user_id = user_id,
                            proposal_id = prop_id,
                            instrument_id = inst_id,
                            file_to_upload = file_to_upload,
                            source_folder = D['source_folder'],
                            creation_time = D['creation_time'],
                            size = int(D['size']),
                            data_format = data_format,
                            public = public_status,
                            raw_or_derived = raw_or_derived, 
                            measurement = measurement
                        )
        print("added dataset...")
        
        # Dataset keywords
        ds = add_keyword_records(session, D['keywords'], ds)
        print("added kw")
        
        # Dataset thumbnails
        if 'thumbnails' in list(D.keys()):
            ds = add_thumbnail_records(session, D['thumbnails'], ds)
        print("added thumnbnails")

        # Dataset files
        if 'associated_files' in list(D.keys()):
            for k,v in D['associated_files'].items():
                
                af_obj = add_or_update(session, AssociatedFile, where=(AssociatedFile.filename == k),
                                       filename = k,
                                       size = int(v['size']), 
                                       sha256_hash = v['sha256_hash'])

                if not (af_obj in ds.associated_files):  
                    ds.associated_files.append(af_obj)
        print("added associated files")

        # ============================= INGESTION RUN
        if raw_or_derived == 'raw' and D['flow_run_id'] is not None:
            print(f"{D['flow_run_id']=}")
            ig_obj = add_or_update(session, IngestionRun, where = (IngestionRun.flow_run_id == D['flow_run_id']), 
                                  githash = D['githash'],
                                  flow_run_id = D['flow_run_id'],
                                  overwrite_data = D['overwrite_data'],
                                  patch_from_google = D['patch_from_google'])



        # ============================= ACCESS CONTROL
        if user:
            print(f"has user {ds.dataset_name=} {user['lbl_email']}")
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == user['orcid'])).one_or_none()
            S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                  .where(DataSetACL.dataset == ds)
            acl = session.exec(S).one_or_none()
            if not acl:
                acl = DataSetACL(accessgroup=ag, dataset=ds)
            acl.read = True
            acl.write= True
            session.add(acl)
        if prop:
            S = select(AccessGroup).where(AccessGroup.group_name == f"MFP{prop['id']:05d}")
            ag = session.exec(S).one_or_none()
            S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                  .where(DataSetACL.dataset == ds)
            acl = session.exec(S).one_or_none()
            if not acl:
                acl = DataSetACL(accessgroup=ag, dataset=ds)
            acl.read = True
            acl.write= False
            session.add(acl)

        # add admin and readonly admin groups
        S = select(AccessGroup).where(AccessGroup.id == 7328)
        ag = session.exec(S).one_or_none()
        S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                  .where(DataSetACL.dataset == ds)
        acl = session.exec(S).one_or_none()
        if not acl:
            acl = DataSetACL(accessgroup=ag, dataset=ds)
        acl.read = True
        acl.write= False
        session.add(acl)
        
        # add admin and readonly admin groups
        S = select(AccessGroup).where(AccessGroup.id == 7329)
        ag = session.exec(S).one_or_none()
        S = select(DataSetACL).where(DataSetACL.accessgroup == ag)\
                                  .where(DataSetACL.dataset == ds)
        acl = session.exec(S).one_or_none()
        if not acl:
            acl = DataSetACL(accessgroup=ag, dataset=ds)
        acl.read = True
        acl.write= True
        session.add(acl)


        
        # ============================= ID TYPE
        add_or_update(session, IdType, where=(IdType.unique_id == uid),
                                       unique_id = uid,
                                       type = "dataset")
        session.commit()



# =============================== STARTING PROCESSES ENDPOINTS


# patch with any dict
@app.patch("/dataset/{pid}")
def update_dataset(pid: str, new_data: Dict[str, Any], ag_ids:  Annotated[str, Depends(authorize_user)]):           
    """
    Update a dataset record in the database.
    **This will send a message to the RMQ server to update the dataset in cloud storage
    Args:
        pid: Dataset ID
        new_data: Updated dataset information
        ag_ids: List of access group IDs the user belongs to; used to confirm they have permission to update the dataset
        
    Returns:
        Updated dataset object
    """

    with Session(engine) as session:
        D_ACL = session.exec(select(DataSetACL).select_from(join(DataSetACL, DataSet, DataSetACL.dataset_id == DataSet.id)).where(DataSet.unique_id == pid)).all()
        
    update_data = {}
    update_data['updated_info'] = new_data
    update_data["dataset_to_update"] = pid

    # connect to RMQ server
    import pika

    rmq_pw = get_secret_2("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")

    # connect to RMQ server
    credentials = pika.PlainCredentials('admin', rmq_pw)
    parameters = pika.ConnectionParameters('10.128.0.31', 5672, '/', credentials)

    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    
    # create a queue to send the message to
    channel.queue_declare(queue='dsupdate')
    
    # send the messsage based on input from CLI args
    message = json.dumps(update_data)
    channel.basic_publish(exchange='',
                          routing_key='dsupdate',
                          body=message)

    connection.close()
    return(f" [x] Sent {message}")

    

@app.patch("/dataset_owner/{pid}")
def update_dataset_owner(pid: str, new_owner: Owner, ag_ids:  Annotated[str, Depends(authorize_user)]):           
    """
    Update the owner of a dataset record in the database.
    **This will send a message to the RMQ server to update the dataset in cloud storage

    Args:
        pid: Dataset ID
        new_owner: New owner information
        ag_ids: List of access group IDs the user belongs to
        
    Returns:
        Updated dataset object
    """
    update_data = {}
    update_data['updated_ownership_info'] = new_owner.dict()
    update_data["dataset_to_update"] = pid
    print(update_data)
    
    import pika

    rmq_pw = get_secret_2("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")

    # connect to RMQ server
    credentials = pika.PlainCredentials('admin', rmq_pw)
    parameters = pika.ConnectionParameters('10.128.0.31', 5672, '/', credentials)

    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    
    # create a queue to send the message to
    channel.queue_declare(queue='dsupdate-owner')
    
    # send the messsage based on input from CLI args
    message = json.dumps(update_data)
    channel.basic_publish(exchange='',
                          routing_key='dsupdate-owner',
                          body=message)

    # close connection to server
    connection.close()
    
    return(message)


@app.post("/dataset_upload/")
async def create_upload_file(file: Annotated[UploadFile, File(description="A file read as UploadFile")],
                             ag_ids:  Annotated[str, Depends(authorize_user)]):
    """
    Upload a file to the database.
    The max size of the file is currently set to 500MB.
    Args:
        file: File to be uploaded
        ag_ids: List of access group IDs the user belongs to; used to confirm they have permission to upload the file
        
    Returns:
        File information
    """
    
    save_file_path = f"/root/uploads/{file.filename}"
    with open(save_file_path, "wb") as f:
        f.write(await file.read())
    return {"filename": file.filename}


@app.post("/dataset_upload_with_md/")
async def create_upload_file(file: Annotated[UploadFile, File(description="A file read as UploadFile")],
                             ag_ids:  Annotated[str, Depends(authorize_user)],
                             metadata_dict: Annotated[str, Form()]):

    form_data_dict = json.loads(metadata_dict)
    form_data_obj = FormData(data=form_data_dict)
    
    md_save_file_path = f"/root/uploads/{file.filename}.json"
    with open(md_save_file_path, "w") as f2:
        json.dump(form_data_obj.data, f2, indent = 4)


    save_file_path = f"/root/uploads/{file.filename}"
    with open(save_file_path, "wb") as f:
        f.write(await file.read())

    return {"filename": file.filename}


@app.post("/dataset_transfer/{pid}")
def launch_dataset_transfer(ag_ids: Annotated[str, Depends(authorize_user)], pid: str , storage_bucket: str):
    
    ds_cmd = select(DataSet).select_from(join(DataSet, DataSetACL, DataSetACL.dataset_id == DataSet.id)).options(defer(DataSet.scientific_metadata)).where(DataSet.unique_id == pid).filter(DataSetACL.accessgroup_id.in_(ag_ids))
    
    with Session(engine) as session:
        ds_list = session.exec(ds_cmd).all()
    
    if ds_list is None:
        raise HTTPException(status_code=402, detail="Dataset not found")
    elif len(ds_list) == 0:
        raise HTTPException(status_code=402, detail="Dataset not found")
    else:
        import pika
        rmq_pw = get_secret_2("RABBITMQ_DEFAULT_PW", "projects/776258882599/secrets/rabbitmq_default_pw/versions/1")
    
        # connect to RMQ server
        credentials = pika.PlainCredentials('admin', rmq_pw)
        parameters = pika.ConnectionParameters('10.128.0.31', 5672, '/', credentials)
    
        connection = pika.BlockingConnection(parameters)
        channel = connection.channel()
        
        # create a queue to send the message to
        channel.queue_declare(queue='transfer-to-drive')
        
        # send the messsage based on input from CLI args
        message = json.dumps({"dsid": pid, "source_bucket": storage_bucket})
        
        channel.basic_publish(exchange='',
                              routing_key='transfer-to-drive',
                              body=message)
    
        # close connection to server
        connection.close()
        
        return(message)


@app.post("/delete_dataset/{pid}")
def delete_dataset(pid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    from sqlmodel import select, delete
    # from pycrucible.models import DataSetKeywordLink, DataSetAssociatedFileLink, DataSetThumbnailLink, DataSetAcl
    with Session(engine) as session:
        # get internal id
        statement = select(DataSet).where(DataSet.unique_id == pid)
        found_ds = session.exec(statement).one_or_none()
        if found_ds is None:
            return("no dataset to delete")
        
        intid = found_ds.id
        
        # delete from all linked tables
        link_tables = [DataSetKeywordLink, DataSetAssociatedFileLink, DataSetThumbnailLink, DataSetACL]
        for tbl in link_tables:
            delete_link(tbl, intid, session)
        delete_link(DataDriveLocation, pid, session)
        session.delete(found_ds)
        session.commit()
        return(pid)





# ==========================  READ NON-DATASET INFO ENDPOINTS
@app.get("/dataset_access_for_user/{pid}/{orcid}")
def read_dataset_access_for_user(pid: str, orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    from sqlmodel import select
    with Session(engine) as session:
        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        print(ag_ids)
        ds_acl = session.exec(select(DataSetACL).select_from(join(DataSetACL, DataSet, DataSetACL.dataset_id == DataSet.id)).where(DataSet.unique_id == pid).filter(DataSetACL.accessgroup_id.in_(ag_ids))).all()
        if len(ds_acl) > 0:
            return True
        else:
            return False
    

@app.get("/user/{orcid}/datasets")
def read_user_datasets(orcid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    from sqlmodel import select
    with Session(engine) as session:
        ag = session.exec(select(UserAccessGroupLink).select_from(join(UserAccessGroupLink, User, User.id == UserAccessGroupLink.user_id)).where(User.orcid == orcid)).all()
        ag_ids = [x.accessgroup_id for x in ag]
        ds_list =session.exec(select(DataSet).select_from(join(DataSetACL, DataSet, DataSetACL.dataset_id == DataSet.id)).where(DataSetACL.accessgroup_id.in_(ag_ids))).all()
        return ds_list


@app.get("/dataset/{pid}/access_groups")
def read_dataset_access(pid: str, admin_acct: Annotated[str, Depends(is_admin)]):
    from sqlmodel import select
    with Session(engine) as session:
        ds_acl = session.exec(select(AccessGroup).select_from(join(AccessGroup, join(DataSetACL, 
                                                                                        DataSet, DataSetACL.dataset_id == DataSet.id),
                                                                                AccessGroup.id == DataSetACL.accessgroup_id)).where(DataSet.unique_id == pid)).all()
        return ds_acl


@app.get("/users/", response_model = List[Tuple])
def read_users(admin_acct:  Annotated[str, Depends(is_admin)]):
    with Session(engine) as session:
        users = session.exec(select(User)).all()
        return users


@app.get("/users/{orcid}")
def read_user(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    from sqlmodel import select
    with Session(engine) as session:
        users = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        return users
        
        
@app.get("/users_by_name/{name}", response_model = List[User])
def read_user(name: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    with Session(engine) as session:
        last_name = name.split(" ")[-1]
        first_name = name.split(" ")[0]
        users = session.exec(select(User).where(User.last_name.like(f"%{last_name}%"))).all()
        users2 = session.exec(select(User).where(User.first_name.like(f"%{first_name}%"))).all()
        all_found = [x[0] for x in users + users2]
        return all_found


@app.get("/users_by_email/{email}", response_model = List[User])
def read_user(email: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    with Session(engine) as session:
        users = session.exec(select(User).where(User.lbl_email.ilike(f"%{email}%"))).all()
        users2 = session.exec(select(User).where(User.email.ilike(f"%{email}%"))).all()
        all_found = [x[0] for x in users + users2]
        return all_found


@app.get("/proposals_for_user/{orcid}", response_model = List[Proposal])
def read_user_proposals(orcid: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    from sqlmodel import select
    with Session(engine) as session:
        user = session.exec(select(User).where(User.orcid == orcid)).one_or_none()
        user_id = user.id
        proposals = session.exec(select(Proposal).select_from(join(Proposal, UserProposalLink, Proposal.id == UserProposalLink.proposal_id)).where(UserProposalLink.user_id == user_id)).all()
        #prop_list = [x[0] for x in proposals]
        return proposals


@app.get("/proposal/{prop_id}")
def read_proposal(prop_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    from sqlmodel import select
    with Session(engine) as session:
        prop = session.exec(select(Proposal).where(Proposal.id == prop_id)).one_or_none()
        return(prop)
        # if prop is not None:
        #     return prop[0]
        # else:
        #     return prop


@app.get("/users_on_proposal/{prop_id}", response_model = List[User])
def read_users_on_proposal(prop_id: str, admin_acct:  Annotated[str, Depends(is_admin)]):
    with Session(engine) as session:
        from sqlmodel import select
        from sqlalchemy.sql import text
        users = session.exec(select(User).select_from(join(User, UserProposalLink, UserProposalLink.user_id == User.id)).where(UserProposalLink.proposal_id == prop_id)).all()
        print(users)
        #user_list = [x[0] for x in users]
        return users


@app.get("/instrument_by_name/{instrument_name}")
def read_instrument(instrument_name: str, ag_ids: Annotated[str, Depends(authorize_user)]):
    from sqlmodel import select
    with Session(engine) as session: 
        iname = instrument_name.lower().strip()
        statement = select(Instrument).where(Instrument.instrument_name == iname)
        inst = session.exec(statement).one_or_none()
        
    return(inst)
        # if inst is not None:
        #     return(inst[0])
        # else:
        #     return inst


@app.get("/list_instruments/", response_model = List)
def read_instrument(ag_ids: Annotated[str, Depends(authorize_user)]):
    with Session(engine) as session: 
        
        statement = select(Instrument)
        all_inst = session.exec(statement).all()

        return(all_inst)

@app.get("/driveid_by_proposal/{proposal}")
def get_drive_id(ag_ids:Annotated[str, Depends(authorize_user)], proposal:str):
    from sqlmodel import select
    with Session(engine) as session:
        statement = select(GoogleDrives).where(GoogleDrives.drive_name == proposal)
        drive_obj = session.exec(statement).one_or_none()
        if drive_obj is not None:
            drive_id = drive_obj.drive_id
            return(drive_id)
        else:
            return(None)


# =============================== READ DATASET ENDPOINTS

@app.get("/dataset_with_metadata/{pid}")
def read_dataset(ag_ids:Annotated[str, Depends(authorize_user)], pid:str):
    print(pid)
    from sqlmodel import select
    with Session(engine) as session:
        ds_cmd = select(DataSet).select_from(join(DataSet, DataSetACL, DataSetACL.dataset_id == DataSet.id)).where(DataSet.unique_id == pid).filter(DataSetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        return ds


@app.get("/dataset/{pid}")
def read_dataset(ag_ids:Annotated[str, Depends(authorize_user)], pid:str):
    from sqlmodel import select
    with Session(engine) as session:
        ds_cmd = select(DataSet).select_from(join(DataSet, DataSetACL, DataSetACL.dataset_id == DataSet.id)).options(defer(DataSet.scientific_metadata)).where(DataSet.unique_id == pid).filter(DataSetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        #print(ds[0])
        return ds
        


@app.get("/dataset_by_name/{dsname}", response_model = List[Tuple])
def read_dataset(ag_ids:  Annotated[str, Depends(authorize_user)], dsname: str):
    with Session(engine) as session:
        ds_cmd = select(DataSet).select_from(join(DataSet, DataSetACL, DataSetACL.dataset_id == DataSet.id)).options(defer(DataSet.scientific_metadata)).where(DataSet.dataset_name == dsname).filter(DataSetACL.accessgroup_id.in_(ag_ids))
        ds_list = session.exec(ds_cmd).all()
        print(ds_list)
        return ds_list
        

### ======== LIST DATASETS
@app.get("/list_datasets", response_model=List)
def list_datasets(ag_ids:  Annotated[str, Depends(authorize_user)], request: Request):
    query_params = request.query_params
    params_dict = dict(query_params)
    from sqlmodel import select
    with Session(engine) as session:

        ds_cmd = select(DataSet).select_from(
                                        join(DataSet, DataSetACL, DataSetACL.dataset_id == DataSet.id))\
                                            .options(defer(DataSet.scientific_metadata))\
                                            .filter(DataSetACL.accessgroup_id.in_(ag_ids))

        for k, v in params_dict.items():
            if hasattr(DataSet, k):
                ds_cmd = ds_cmd.where(getattr(DataSet, k) == v)
        print(ds_cmd)
        ds_list = session.exec(ds_cmd).all()
       # ds_list_edit = [x[0] for x in ds_list]
        return(ds_list)


@app.get("/datasets_by_keyword/{keyword}", response_model = List)
def list_datasets_by_keyword(ag_ids:  Annotated[str, Depends(authorize_user)], keyword: str):
    from sqlmodel import select
    with Session(engine) as session:
        
        K = session.exec(select(Keyword).where(Keyword.keyword.ilike(f"%{keyword}%"))).all()
        
        if K:
            k_ids = [k.id for k in K]
            ds_cmd = select(DataSet).select_from(join(DataSetKeywordLink,
                                                join(DataSet, DataSetACL, DataSetACL.dataset_id == DataSet.id),
                                                     DataSetKeywordLink.dataset_id == DataSet.id))\
                                                .options(defer(DataSet.scientific_metadata))\
                                                .filter(DataSetACL.accessgroup_id.in_(ag_ids))\
                                                .filter(DataSetKeywordLink.keyword_id.in_(k_ids))
            
            ds_list = session.exec(ds_cmd).all()
           # ds_list_edit = [x[0] for x in ds_list]
            return(ds_list)
        else:
            return None

@app.get("/dataset_location/{pid}", response_model = List)
def read_dataset(ag_ids:  Annotated[str, Depends(authorize_user)], pid: str):
    from sqlalchemy.sql import text
    from sqlmodel import select
    with Session(engine) as session:
        ds_locs = session.exec(select(DataDriveLocation).where(and_(DataDriveLocation.dataset_id == pid, 
                                                                    DataDriveLocation.status == 'current'))).all()
        #ds_locs_edit = [x[0] for x in ds_locs]
        return(ds_locs)






















