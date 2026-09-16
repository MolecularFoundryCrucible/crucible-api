import pytest
from fastapi import HTTPException

from app.api.routes.projects import _validate_service_account_project_role
from app.models.core_models import User
from app.utils.permissions import PlatformRole, Role


def _user(*, is_service_account: bool, platform_role: PlatformRole) -> User:
    return User(
        first_name="",
        last_name="",
        email=None,
        unique_id="principal-one",
        username="principal-one",
        is_service_account=is_service_account,
        platform_role=platform_role.value,
    )


@pytest.mark.parametrize("role", [Role.VIEWER, Role.CONTRIBUTOR])
def test_non_admin_service_account_accepts_roles_at_or_below_contributor(role):
    user = _user(is_service_account=True, platform_role=PlatformRole.CONTRIBUTOR)
    _validate_service_account_project_role(user, role)


@pytest.mark.parametrize("role", [Role.EDITOR, Role.ADMIN, Role.OWNER])
def test_non_admin_service_account_rejects_roles_above_contributor(role):
    user = _user(is_service_account=True, platform_role=PlatformRole.CONTRIBUTOR)
    with pytest.raises(HTTPException) as exc:
        _validate_service_account_project_role(user, role)
    assert exc.value.status_code == 422


def test_platform_admin_service_account_can_hold_higher_project_role():
    user = _user(is_service_account=True, platform_role=PlatformRole.ADMIN)
    _validate_service_account_project_role(user, Role.ADMIN)


def test_human_project_roles_are_unchanged():
    user = _user(is_service_account=False, platform_role=PlatformRole.NONE)
    _validate_service_account_project_role(user, Role.ADMIN)
