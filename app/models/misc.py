from typing import Optional, List
from sqlalchemy import JSON, Column,BigInteger
from sqlmodel import Field, SQLModel,Relationship


class IdType(SQLModel, table = True):
    unique_id: str = Field(default = None, primary_key = True,index = True)
    object_type: str


class ApiToken(SQLModel, table = True):
    access_group_id: str = Field(default = None, primary_key = True, index = True)
    token: str = Field(default = None, primary_key = True, index = True)
    token_created: str



    


