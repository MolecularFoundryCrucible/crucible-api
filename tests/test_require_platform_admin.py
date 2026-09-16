"""Platform-wide bypass checks for platform-level routes."""
import pytest
from fastapi import HTTPException

from app.utils.auth_utils import require_platform_admin
from app.utils.permissions import GroupMembership, PlatformRole, Principal, Role


def principal(*, platform_role=PlatformRole.NONE, groups=(), is_sa=False):
    return Principal(user_id="0000-0001", platform_role=platform_role,
                     is_service_account=is_sa, groups=tuple(groups))


ADMIN_GROUP = GroupMembership(1, "admin", Role.VIEWER)
PROJECT_GROUP = GroupMembership(2, "proj_x", Role.OWNER)


class TestAllows:
    def test_platform_role_admin(self):
        require_platform_admin(principal(platform_role=PlatformRole.ADMIN))

    def test_service_account_with_platform_role(self):
        """The promoted service accounts must keep working."""
        require_platform_admin(principal(platform_role=PlatformRole.ADMIN, is_sa=True))


class TestDenies:
    @pytest.mark.parametrize("p,why", [
        (principal(), "no groups, no platform role"),
        (principal(groups=[PROJECT_GROUP]), "a project owner is not a platform admin"),
        (principal(platform_role=PlatformRole.SUPPORT), "support is not admin"),
        (principal(platform_role=PlatformRole.NONE, groups=[PROJECT_GROUP]), "both absent"),
        (principal(groups=[ADMIN_GROUP]), "admin group membership is not platform authority"),
    ])
    def test_rejects(self, p, why):
        with pytest.raises(HTTPException) as exc:
            require_platform_admin(p)
        assert exc.value.status_code == 403, why

    def test_owner_of_everything_is_still_not_a_platform_admin(self):
        """Resource roles never confer platform bypass, however high."""
        p = principal(groups=[GroupMembership(3, "proj_y", Role.OWNER),
                              GroupMembership(4, "proj_z", Role.OWNER)])
        with pytest.raises(HTTPException):
            require_platform_admin(p)

    def test_service_account_admin_group_does_not_bypass_contributor_ceiling(self):
        with pytest.raises(HTTPException):
            require_platform_admin(principal(groups=[ADMIN_GROUP], is_sa=True))

    @pytest.mark.parametrize("name", ["admins", "Admin", "ADMIN", "admin2", "read_only"])
    def test_group_named_like_admin_does_not_count(self, name):
        """Exact match only."""
        with pytest.raises(HTTPException):
            require_platform_admin(principal(groups=[GroupMembership(9, name, Role.OWNER)]))


def test_returns_none_rather_than_a_truthy_value():
    """It is a guard, not a predicate — `if require_platform_admin(p):` would
    silently never fire."""
    assert require_platform_admin(principal(platform_role=PlatformRole.ADMIN)) is None
