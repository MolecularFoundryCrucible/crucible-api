from typing import Optional
from pydantic import Field, field_validator
from sqlmodel import SQLModel
from app.utils.user_utils import validate_username_format


class UserCreate(SQLModel):
    '''Input schema for creating a user.'''
    model_config = {"populate_by_name": True}

    first_name: str
    last_name: str
    unique_id: str = Field(validation_alias="orcid")
    email: Optional[str] = None
    username: Optional[str] = None
    is_service_account: Optional[bool] = False

    @field_validator("username", mode="before")
    @classmethod
    def check_username(cls, v):
        if v is None:
            return v
        return validate_username_format(str(v))


class UserRead(SQLModel):
    first_name: str
    last_name: str
    email: Optional[str] = None
    unique_id: str
    username: Optional[str] = None
    is_service_account: Optional[bool] = False


class UserPublicRead(SQLModel):
    '''Public-safe user representation. Never includes email.
    Used in search results and any context where a user views another user's info.
    '''
    unique_id: str
    username: Optional[str] = None
    first_name: str
    last_name: str


class AccountProfileUpdate(SQLModel):
    '''Self-service profile update. Does not allow changing is_service_account.'''
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None

    @field_validator("username", mode="before")
    @classmethod
    def check_username(cls, v):
        if v is None:
            return v
        return validate_username_format(str(v))


class UserUpdate(SQLModel):
    '''Admin partial update schema for User. All fields may be modified.'''
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    is_service_account: Optional[bool] = None

    @field_validator("username", mode="before")
    @classmethod
    def check_username(cls, v):
        if v is None:
            return v
        return validate_username_format(str(v))
