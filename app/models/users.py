from typing import Any, List, Optional
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
    username: str
    is_service_account: Optional[bool] = False

    @field_validator("username", mode="before")
    @classmethod
    def check_username(cls, v):
        return validate_username_format(str(v))

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v):
        if v is None:
            return v
        return str(v).lower()


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

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v):
        if v is None:
            return v
        return str(v).lower()


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

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, v):
        if v is None:
            return v
        return str(v).lower()


class ServiceAccountCreate(SQLModel):
    '''Request body for POST /service_accounts.
    username is required and must be unique across all users.
    unique_id is optional — if omitted the server generates an mfid.
    '''
    username: str
    unique_id: Optional[str] = None

    @field_validator("username", mode="before")
    @classmethod
    def check_username(cls, v):
        return validate_username_format(str(v))


class ServiceAccountRead(SQLModel):
    '''Response from POST /service_accounts and POST /service_accounts/{id}/rotate_key.
    api_key is only returned once — store it securely.
    '''
    unique_id: str
    username: str
    is_service_account: bool = True
    api_key: str
