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


# =============================== datasets

@app.get("/dataset_with_metadata/{pid}")
def read_dataset(ag_ids:Annotated[str, Depends(authorize_user)], pid:str):
    print(pid)
    from sqlmodel import select
    with Session(engine) as session:
        ds_cmd = select(DataSet).select_from(join(DataSet, DataSetACL, DataSetACL.dataset_id == DataSet.id)).where(DataSet.unique_id == pid).filter(DataSetACL.accessgroup_id.in_(ag_ids))
        ds = session.exec(ds_cmd).one_or_none()
        return ds


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















