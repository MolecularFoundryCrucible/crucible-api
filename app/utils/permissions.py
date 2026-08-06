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
    """Whether the principal may perform `permission` on the resource.

    Platform admins bypass before any ACL is consulted, which is why an admin
    needs no ACL rows and can reach resources that do not exist yet.
    """
    if principal.is_platform_admin:
        return True
    role = effective_role(principal, acl_rows)
    return role is not None and role >= THRESHOLDS[permission]


def can_grant(
    actor_role: Optional[Role],
    target_role: Role,
    current_role: Optional[Role] = None,
) -> bool:
    """Whether someone at `actor_role` may set an entry to `target_role`.

    The cap rule, applied identically to ACL entries and project membership,
    and it binds on *both* ends:

    - `target_role` at or below your own, so an admin cannot mint an owner.
    - `current_role` at or below your own, so an admin cannot *overwrite* an
      owner's entry either. Pass the role being replaced whenever one exists;
      omitting it checks only the creation direction.

    The second half is what the design means by "an owner's ACL entry cannot be
    modified by an admin". Without it a project admin could demote the lead to
    viewer, since viewer clears the target check on its own. Removal is the
    same operation as assignment for this purpose — pass `current_role` and
    whatever the entry would become.
    """
    if actor_role is None:
        return False
    if actor_role < THRESHOLDS[Permission.MANAGE_ACL]:
        return False
    if target_role > actor_role:
        return False
    if current_role is not None and current_role > actor_role:
        return False
    return True
