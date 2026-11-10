from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel


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


class EntryCapabilities(SQLModel):
    """What the caller may do to one member or grant, as the matching routes would decide."""
    can_remove: bool = Field(description="The matching DELETE would succeed: removal up to your own role, or leaving yourself.")
    can_change_role: bool = Field(description="The matching PATCH or PUT would succeed for at least one other role.")
    assignable_roles: list[AccessPermission] = Field(description="Roles the matching PATCH or PUT would accept for this entry.")


class AccessGrantRead(SQLModel):
    """One canonical principal grant on a resource."""
    principal_id: str
    principal_type: AccessPrincipalType
    permission: AccessPermission
    slug: Optional[str] = None
    display_name: Optional[str] = None
    capabilities: Optional[EntryCapabilities] = None


class AccessGrantWrite(SQLModel):
    """Body for PUT /resources/{mfid}/access/{kind}/{principal}."""
    permission: AccessPermission


class ResourceAccessGroupRead(SQLModel):
    """The reply of the deprecated POST /resources/{mfid}/access_groups."""
    mfid: str
    accessgroup_id: Optional[int] = None
    read: bool
    write: bool
    permission: str
