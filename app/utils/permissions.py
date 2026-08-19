"""Pure permission model — no database, no HTTP, no FastAPI.

Everything here is a plain value or a function over plain values, which is what
makes the combinatorial matrix in tests/test_permissions.py cheap to run
exhaustively. The fetch-and-raise layer lives in auth_utils.

Design reference: docs/permission_system_plan.md
"""
from dataclasses import dataclass
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
    SUPPORT = "support"
    ADMIN = "admin"


class Permission(str, Enum):
    READ = "read"
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"
    MANAGE_ACL = "manage_acl"
    TRANSFER = "transfer"


_THRESHOLDS: dict[Permission, Role] = {
    Permission.READ: Role.VIEWER,
    Permission.CREATE: Role.CONTRIBUTOR,
    Permission.EDIT: Role.EDITOR,
    Permission.DELETE: Role.ADMIN,
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
    """Whether the principal may perform `permission`. Platform admins bypass
    before any ACL is consulted, so they need no rows."""
    if principal.is_platform_admin:
        return True
    role = effective_role(principal, acl_rows)
    return role is not None and role >= THRESHOLDS[permission]


def can_grant(
    actor_role: Optional[Role],
    target_role: Role,
    current_role: Optional[Role] = None,
    floor: Role = Role.ADMIN,
) -> bool:
    """Whether `actor_role` may set an entry to `target_role`, replacing
    `current_role`.

    The cap binds on both ends: an admin can neither mint an owner nor
    overwrite one. Omitting `current_role` checks only the creation direction,
    which would let an admin demote a lead to viewer.

    `floor` is the minimum role that may manage at all, and differs by what is
    being managed: `admin` for ACL entries, which share data outward without
    bound, `editor` for project membership, which is bounded by the project.
    """
    if actor_role is None:
        return False
    if actor_role < floor:
        return False
    if target_role > actor_role:
        return False
    if current_role is not None and current_role > actor_role:
        return False
    return True
