from typing import Any, List, Optional
from pydantic import AliasChoices, ConfigDict, Field, field_validator
from sqlmodel import SQLModel
from app.general_utils import is_orcid
from app.utils.permissions import PlatformRole
from app.utils.user_utils import validate_username_format


class UserCreate(SQLModel):
    '''Input schema for creating a user.'''
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    first_name: str
    last_name: str
    unique_id: Optional[str] = Field(
        default=None,
        validation_alias=AliasChoices("unique_id", "orcid"),
        description="Human ORCID. When omitted, the server assigns an MFID.",
    )
    email: Optional[str] = None
    username: str

    @field_validator("username", mode="before")
    @classmethod
    def check_username(cls, v):
        return validate_username_format(str(v))

    @field_validator("unique_id", mode="before")
    @classmethod
    def check_unique_id(cls, v):
        if v is None:
            return None
        value = str(v).strip()
        if not is_orcid(value):
            raise ValueError("unique_id must be an ORCID when supplied; omit it to generate an MFID")
        return value

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


class AccountCapabilities(SQLModel):
    can_manage_service_accounts: bool
    can_create_project: bool
    can_register_instrument: bool
    can_create_sample: bool
    can_create_dataset: bool
    can_create_for_others: bool


class AccountProfileRead(UserRead):
    capabilities: AccountCapabilities


class UserPublicRead(SQLModel):
    '''Public-safe user representation. Never includes email.
    Used in search results and any context where a user views another user's info.
    '''
    unique_id: str
    username: Optional[str] = None
    first_name: str
    last_name: str
    is_service_account: bool = False

    @field_validator("is_service_account", mode="before")
    @classmethod
    def normalize_service_account(cls, v):
        return False if v is None else v


class AccountProfileUpdate(SQLModel):
    '''Self-service profile update. Does not allow changing is_service_account.'''
    model_config = ConfigDict(extra="forbid")

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
    '''Admin partial update schema for mutable user profile fields.'''
    model_config = ConfigDict(extra="forbid")

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


class ServiceAccountAdminRead(SQLModel):
    unique_id: str
    username: str
    first_name: str
    last_name: str
    email: Optional[str] = None
    is_service_account: bool = True
    platform_role: PlatformRole


class ServiceAccountKeyStatus(SQLModel):
    valid: bool
    created_at: str
    expires_at: str


class ServiceAccountAdminDetail(ServiceAccountAdminRead):
    api_key_status: Optional[ServiceAccountKeyStatus] = None
