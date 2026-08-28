from enum import Enum
from typing import Optional

from sqlmodel import SQLModel


class AccessPrincipalType(str, Enum):
    USER = "user"
    SERVICE_ACCOUNT = "service_account"
    PROJECT = "project"
    INSTRUMENT = "instrument"
    PUBLIC = "public"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class AccessPermission(str, Enum):
    VIEWER = "viewer"
    CONTRIBUTOR = "contributor"
    EDITOR = "editor"
    ADMIN = "admin"
    OWNER = "owner"


class EffectiveAccess(str, Enum):
    NONE = "none"
    VIEWER = "viewer"
    CONTRIBUTOR = "contributor"
    EDITOR = "editor"
    ADMIN = "admin"
    OWNER = "owner"
    PLATFORM_ADMIN = "platform_admin"


class EffectiveResourceAccessRead(SQLModel):
    resource_mfid: str
    user_id: str
    effective_access: EffectiveAccess


class AccessGrantRead(SQLModel):
    """One canonical principal grant on a resource."""
    principal_id: str
    principal_type: AccessPrincipalType
    permission: AccessPermission
    slug: Optional[str] = None
    display_name: Optional[str] = None


class AccessGrantWrite(SQLModel):
    """Body for PUT /resources/{mfid}/access/{kind}/{principal}."""
    permission: AccessPermission
