from typing import Any, List, Literal, Optional
from pydantic import MISSING, AliasChoices, ConfigDict, Field, field_validator
from sqlmodel import SQLModel
from app.general_utils import is_orcid, is_valid_mfid
from app.utils.permissions import PlatformRole, PrivilegeMode
from app.utils.user_utils import validate_username_format
from app.models.fields import NonBlankStr
from app.models.timestamps import UtcDatetime


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
        return str(v).strip().lower() or None


class UserCapabilities(SQLModel):
    '''Caller-relative actions on one user record. Field name is user_capabilities,
    not capabilities: a downstream client decodes any capabilities key on user
    objects into a different required-field type.'''
    can_view_private: bool
    can_edit_profile: bool
    can_manage_api_key: bool
    can_change_platform_role: bool


class ServiceAccountKeyStatus(SQLModel):
    valid: bool
    created_at: str
    expires_at: str
    last_used_at: Optional[UtcDatetime] = Field(default=None, description="Last authenticated use, recorded at most every 10 minutes.")


class UserRead(SQLModel):
    first_name: str
    last_name: str
    email: Optional[str] = None
    unique_id: str
    username: Optional[str] = None
    is_service_account: Optional[bool] = False
    platform_role: Optional[PlatformRole] = None
    user_capabilities: Optional[UserCapabilities] = None
    api_key_status: Optional[ServiceAccountKeyStatus] = None
    last_active_at: Optional[UtcDatetime] = Field(default=None, description="Last authenticated request, recorded at most hourly. Returned to platform administrators and to the user themselves.")


class AccountCapabilities(SQLModel):
    can_manage_service_accounts: bool
    can_manage_users: bool
    can_create_project: bool
    can_register_instrument: bool
    can_create_sample: bool
    can_create_dataset: bool
    can_create_for_others: bool


class AccountAuthorizationRead(SQLModel):
    platform_role: PlatformRole
    effective_privilege_mode: PrivilegeMode
    can_elevate: bool


class AccountProfileRead(UserRead):
    authorization: AccountAuthorizationRead
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
    user_capabilities: Optional[UserCapabilities] = None

    @field_validator("is_service_account", mode="before")
    @classmethod
    def normalize_service_account(cls, v):
        return False if v is None else v


class AccountProfileUpdate(SQLModel):
    '''Self-service profile update. Does not allow changing is_service_account.'''
    model_config = ConfigDict(extra="forbid")

    first_name: NonBlankStr | MISSING = MISSING
    last_name: NonBlankStr | MISSING = MISSING
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
        return str(v).strip().lower() or None


class UserUpdate(SQLModel):
    '''Admin partial update schema for mutable user profile fields.'''
    model_config = ConfigDict(extra="forbid")

    first_name: NonBlankStr | MISSING = MISSING
    last_name: NonBlankStr | MISSING = MISSING
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
        return str(v).strip().lower() or None


class ServiceAccountCreate(SQLModel):
    '''Request body for POST /service_accounts.
    username is required and must be unique across all users.
    unique_id is optional and must be an MFID; if omitted the server generates one.
    '''
    username: str
    unique_id: Optional[str] = None

    @field_validator("username", mode="before")
    @classmethod
    def check_username(cls, v):
        return validate_username_format(str(v))

    @field_validator("unique_id")
    @classmethod
    def check_unique_id(cls, v):
        if v is not None and not is_valid_mfid(v):
            raise ValueError("unique_id must be a 26-character MFID")
        return v


class ServiceAccountPlatformRoleUpdate(SQLModel):
    model_config = ConfigDict(extra="forbid")

    platform_role: Literal["none", "contributor", "admin"]


class UserPlatformRoleUpdate(SQLModel):
    model_config = ConfigDict(extra="forbid")

    platform_role: Literal["none", "contributor", "admin"]


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


class ServiceAccountAdminDetail(ServiceAccountAdminRead):
    api_key_status: Optional[ServiceAccountKeyStatus] = None
