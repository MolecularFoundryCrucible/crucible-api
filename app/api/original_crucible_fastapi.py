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


       


@app.get("/dataset_location/{pid}", response_model = List)
def read_dataset(ag_ids:  Annotated[str, Depends(authorize_user)], pid: str):
    from sqlalchemy.sql import text
    from sqlmodel import select
    with Session(engine) as session:
        ds_locs = session.exec(select(DataDriveLocation).where(and_(DataDriveLocation.dataset_id == pid, 
                                                                    DataDriveLocation.status == 'current'))).all()
        #ds_locs_edit = [x[0] for x in ds_locs]
        return(ds_locs)






















