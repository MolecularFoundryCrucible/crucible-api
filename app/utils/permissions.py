"""Pure permission model — no database, no HTTP, no FastAPI.

Everything here is a plain value or a function over plain values, which is what
makes the combinatorial matrix in tests/test_permissions.py cheap to run
exhaustively. The fetch-and-raise layer lives in auth_utils.

Design reference: docs/permission_system_plan.md
"""
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

PUBLIC_GROUP_NAME = "public"


class Role(IntEnum):
    """Mirrors the Postgres `resource_role` enum. Ascending order is load-bearing:
    comparisons and min() are the model."""
    VIEWER = 1
    CONTRIBUTOR = 2
    EDITOR = 3
    ADMIN = 4
    OWNER = 5

    @property
    def db_value(self) -> str:
        return self.name.lower()

    @classmethod
    def from_db(cls, value) -> "Role":
        if isinstance(value, cls):
            return value
        return cls[str(value).upper()]


class PlatformRole(str, Enum):
    NONE = "none"
    CONTRIBUTOR = "contributor"
    SUPPORT = "support"
    ADMIN = "admin"


class PrivilegeMode(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"


class PrivilegeModeSource(str, Enum):
    DEFAULT = "default"
    EXPLICIT = "explicit"
    COMPATIBILITY_DEFAULT = "compatibility_default"


class AccessGroupType(str, Enum):
    IDENTITY = "identity"
    PROJECT = "project"
    INSTRUMENT = "instrument"
    CUSTOM = "custom"
    SYSTEM = "system"


class Permission(str, Enum):
    READ = "read"
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    HARD_DELETE = "hard_delete"
    MANAGE_ACL = "manage_acl"
    TRANSFER = "transfer"


_THRESHOLDS: dict[Permission, Role] = {
    Permission.READ: Role.VIEWER,
    Permission.CREATE: Role.CONTRIBUTOR,
    Permission.EDIT: Role.EDITOR,
    Permission.DELETE: Role.ADMIN,
    Permission.HARD_DELETE: Role.OWNER,
    Permission.MANAGE_ACL: Role.ADMIN,
    Permission.TRANSFER: Role.OWNER,
}

# Read-only view: a mutable module-level dict of security thresholds is one
# stray assignment away from silently lowering every check in the process.
THRESHOLDS: Mapping[Permission, Role] = MappingProxyType(_THRESHOLDS)


@dataclass(frozen=True)
class GroupMembership:
    """A principal's standing in one access group."""
    id: int
    group_name: str
    role: Role


@dataclass(frozen=True)
class Principal:
    """The caller, as an authorization decision sees them.

    Frozen and ORM-free: safe to share across threads via the auth cache, and
    constructible in a test without a database.
    """
    user_id: str
    platform_role: PlatformRole = PlatformRole.NONE
    is_service_account: bool = False
    groups: tuple[GroupMembership, ...] = ()
    credential: str = "api_key"
    authenticated_at: Optional[datetime] = None
    session_family_id: Optional[str] = None
    internal_user_id: Optional[int] = None

    def is_user(self, user) -> bool:
        """Whether `user`, a User row, is the caller. Compares the internal key, which never
        changes, rather than unique_id."""
        return user is not None and self.has_user_id(user.id)

    def has_user_id(self, user_id) -> bool:
        return self.internal_user_id is not None and user_id == self.internal_user_id

    @property
    def is_platform_admin(self) -> bool:
        return self.platform_role is PlatformRole.ADMIN

    def role_in(self, group_name: str) -> Optional[Role]:
        for m in self.groups:
            if m.group_name == group_name:
                return m.role
        return None

    def group_ids_at_or_above(self, role: Role) -> list[int]:
        """Group ids whose standing alone clears `role`.

        min(a,b) >= r iff a >= r and b >= r, so filtering here lets the SQL side
        compare one column instead of joining useraccessgrouplink.
        """
        return [m.id for m in self.groups if m.role >= role]

    def real_group_names(self) -> list[str]:
        """Group names excluding the virtual public group, which is injected into
        every principal and is not a real membership."""
        return [m.group_name for m in self.groups if m.group_name != PUBLIC_GROUP_NAME]


@dataclass(frozen=True)
class AuthorizationContext:
    actor: Principal
    principal: Principal
    privilege_mode: PrivilegeMode
    mode_source: PrivilegeModeSource

    @property
    def can_elevate(self) -> bool:
        return self.actor.is_platform_admin and not self.actor.is_service_account


def resolve_authorization_context(
    actor: Principal,
    requested_mode: Optional[PrivilegeMode],
) -> AuthorizationContext:
    can_elevate = actor.is_platform_admin and not actor.is_service_account
    if requested_mode is PrivilegeMode.ELEVATED and not can_elevate:
        raise PermissionError("Caller is not eligible for elevated mode")

    if requested_mode is None:
        if actor.is_platform_admin and actor.credential != "hub":
            return AuthorizationContext(
                actor=actor,
                principal=actor,
                privilege_mode=PrivilegeMode.ELEVATED,
                mode_source=PrivilegeModeSource.COMPATIBILITY_DEFAULT,
            )
        return AuthorizationContext(
            actor=actor,
            principal=replace(actor, platform_role=PlatformRole.NONE) if actor.is_platform_admin else actor,
            privilege_mode=PrivilegeMode.NORMAL,
            mode_source=PrivilegeModeSource.DEFAULT,
        )

    if requested_mode is PrivilegeMode.NORMAL:
        principal = (
            replace(actor, platform_role=PlatformRole.NONE)
            if actor.is_platform_admin
            else actor
        )
        return AuthorizationContext(
            actor=actor,
            principal=principal,
            privilege_mode=PrivilegeMode.NORMAL,
            mode_source=PrivilegeModeSource.EXPLICIT,
        )

    return AuthorizationContext(
        actor=actor,
        principal=actor,
        privilege_mode=PrivilegeMode.ELEVATED,
        mode_source=PrivilegeModeSource.EXPLICIT,
    )


def effective_role(
    principal: Principal,
    acl_rows: Iterable[tuple[int, Role]],
) -> Optional[Role]:
    """MAX over the principal's groups of MIN(standing, grant).

    `acl_rows` is (accessgroup_id, granted_role) for one resource. None means no
    access — there is no rung below viewer.

    MIN keeps sharing safe (everyone is `owner` of their own ORCID group, so a
    viewer share must cap). MAX makes ownership survive leaving a project.
    Platform admin is deliberately not considered here — bypass is never an
    effective role.
    """
    grants: dict[int, Role] = {}
    for group_id, granted in acl_rows:
        granted = Role.from_db(granted)
        # Keep the strongest if a group appears twice. The composite primary key
        # on (mfid, accessgroup_id) makes that impossible from resourceacl
        # itself, but acl_rows may arrive from a join that fans out, and
        # silently taking the last row would under-grant.
        if group_id not in grants or granted > grants[group_id]:
            grants[group_id] = granted

    if not grants:
        return None

    best: Optional[Role] = None
    for membership in principal.groups:
        grant = grants.get(membership.id)
        if grant is None:
            continue
        reachable = min(Role.from_db(membership.role), grant)
        if best is None or reachable > best:
            best = reachable
    return best


def has_permission(
    principal: Principal,
    acl_rows: Iterable[tuple[int, Role]],
    permission: Permission,
) -> bool:
    """Whether the principal may perform `permission`."""
    if has_global_permission(principal, permission):
        return True
    role = effective_role(principal, acl_rows)
    if (
        principal.is_service_account
        and role is not None
        and role > Role.CONTRIBUTOR
    ):
        role = Role.CONTRIBUTOR
    return role is not None and role >= THRESHOLDS[permission]


def has_global_permission(principal: Principal, permission: Permission) -> bool:
    if principal.is_platform_admin:
        return True
    return (
        principal.platform_role is PlatformRole.CONTRIBUTOR
        and THRESHOLDS[permission] <= Role.CONTRIBUTOR
    )


def can_grant(
    actor_role: Optional[Role],
    target_role: Role,
    current_role: Optional[Role] = None,
    floor: Role = Role.ADMIN,
    allow_peer: bool = True,
) -> bool:
    """Whether `actor_role` may set an entry to `target_role`, replacing
    `current_role`.

    The cap binds on both ends: an admin can neither mint an owner nor
    overwrite one. Set `allow_peer` to false for strict hierarchies where a
    role may manage only roles below itself.

    `floor` is the minimum role that may manage at all, and differs by what is
    being managed: `admin` for ACL entries, which share data outward without
    bound, `editor` for project membership, which is bounded by the project.
    """
    if actor_role is None:
        return False
    if actor_role < floor:
        return False
    if target_role > actor_role or (not allow_peer and target_role == actor_role):
        return False
    if current_role is not None and (
        current_role > actor_role or (not allow_peer and current_role == actor_role)
    ):
        return False
    return True
