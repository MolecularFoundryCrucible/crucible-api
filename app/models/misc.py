from typing import Optional, List
from sqlalchemy import JSON, Column,BigInteger
from sqlmodel import Field, SQLModel,Relationship


class GoogleDrives(SQLModel, table = True):
    drive_id: str = Field(default=None, primary_key = True)
    drive_name: str = Field(default = None, unique = True, primary_key = True)


class DataDriveLocation(SQLModel, table = True):
    id: Optional[int] = Field(default = None, primary_key = True)
    drive_name: str = Field(default=None, foreign_key="googledrives.drive_name", index = True)
    dataset_id: str = Field(default=None, foreign_key="dataset.unique_id", index = True)
    folder_path_in_drive: str
    folder_id: str = Field(default = None, unique = True)
    url: str
    folder_last_mod: Optional[str]
    status: str


class IdType(SQLModel, table = True):
    unique_id: str = Field(default = None, primary_key = True,index = True)
    object_type: str


class ApiToken(SQLModel, table = True):
    access_group_id: str = Field(default = None, primary_key = True, index = True)
    token: str = Field(default = None, primary_key = True, index = True)
    token_created: str



    


