"""Pure permission model — no database, no HTTP, no FastAPI.

Everything here is a plain value or a function over plain values, which is what
makes the combinatorial matrix in tests/test_permissions.py cheap to run
exhaustively. The fetch-and-raise layer lives in auth_utils.

Design reference: docs/permission_system_plan.md
"""
from dataclasses import dataclass
from enum import Enum, IntEnum
from typing import Iterable, Optional

PUBLIC_GROUP_NAME = "public"


class Role(IntEnum):
    """Ascending order matters: comparisons and min() are the whole model.

    Values mirror the Postgres `resource_role` enum, whose declaration order
    gives the same ordering server-side.
    """
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


THRESHOLDS: dict[Permission, Role] = {
    Permission.READ: Role.VIEWER,
    Permission.CREATE: Role.CONTRIBUTOR,
    Permission.EDIT: Role.EDITOR,
    Permission.DELETE: Role.ADMIN,
    Permission.MANAGE_ACL: Role.ADMIN,
    Permission.TRANSFER: Role.OWNER,
}


@dataclass(frozen=True)
class GroupMembership:
    """A principal's standing in one group. `role` is the standing, not a grant."""
    id: int
    group_name: str
    role: Role


@dataclass(frozen=True)
class Principal:
    """Everything an authorization decision needs about the caller.

    Replaces the anonymous `(orcid, [CachedGroup])` tuple that 106 routes
    currently destructure. Frozen and free of ORM objects, so it is safe to
    cache across threads and a test can build one without a database.
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
        """Group ids where standing alone clears `role`.

        This is the Python half of the list-query filter. Because
        min(a, b) >= r is equivalent to a >= r and b >= r, filtering
        memberships here lets the SQL side compare a single column instead of
        joining useraccessgrouplink.
        """
        return [m.id for m in self.groups if m.role >= role]

    def real_group_names(self) -> list[str]:
        """Group names excluding the virtual public group.

        Anything answering "which projects am I in" must use this — the public
        group is injected into every principal and is not a membership.
        """
        return [m.group_name for m in self.groups if m.group_name != PUBLIC_GROUP_NAME]


def effective_role(
    principal: Principal,
    acl_rows: Iterable[tuple[int, Role]],
) -> Optional[Role]:
    """MAX over the principal's groups of MIN(standing, grant).

    `acl_rows` is (accessgroup_id, granted_role) for one resource. Returns None
    when no group of the principal's has an entry, which means no access rather
    than a low role — there is no rung below viewer.

    The MIN is what makes sharing safe: everyone holds `owner` in their own
    ORCID group, so without it "share with Alice as viewer" would hand Alice
    delete. The MAX is what makes ownership survive leaving a project.

    Deliberately does not consider platform admin. Bypass is never expressed as
    an effective role; require_permission applies it before calling this.
    """
    grants = dict(acl_rows)
    if not grants:
        return None

    best: Optional[Role] = None
    for membership in principal.groups:
        grant = grants.get(membership.id)
        if grant is None:
            continue
        reachable = min(membership.role, Role.from_db(grant))
        if best is None or reachable > best:
            best = reachable
    return best


def has_permission(
    principal: Principal,
    acl_rows: Iterable[tuple[int, Role]],
    permission: Permission = Permission.READ,
) -> bool:
    """Whether the principal may perform `permission` on the resource.

    Platform admins bypass before any ACL is consulted, which is why an admin
    needs no ACL rows and can reach resources that do not exist yet.
    """
    if principal.is_platform_admin:
        return True
    role = effective_role(principal, acl_rows)
    return role is not None and role >= THRESHOLDS[permission]


def can_grant(actor_role: Optional[Role], target_role: Role) -> bool:
    """Whether someone at `actor_role` may hand out `target_role`.

    The cap rule, applied identically to ACL entries and project membership:
    you may manage at or below your own standing and never above it. Without
    it, an admin could mint an owner and escape the ceiling that makes the
    model monotonic.
    """
    if actor_role is None:
        return False
    if actor_role < THRESHOLDS[Permission.MANAGE_ACL]:
        return False
    return target_role <= actor_role
