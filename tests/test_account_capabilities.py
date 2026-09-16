import pytest
from sqlmodel import Session

from app.api.routes.account import _account_capabilities, get_account_profile
from app.models.core_models import User
from app.utils.permissions import PlatformRole, Principal


@pytest.mark.parametrize(
    "principal,expected",
    [
        (
            Principal(user_id="human"),
            {
                "can_manage_service_accounts": False,
                "can_create_project": True,
                "can_register_instrument": True,
                "can_create_sample": True,
                "can_create_dataset": True,
                "can_create_for_others": False,
            },
        ),
        (
            Principal(
                user_id="service-account",
                platform_role=PlatformRole.CONTRIBUTOR,
                is_service_account=True,
            ),
            {
                "can_manage_service_accounts": False,
                "can_create_project": True,
                "can_register_instrument": False,
                "can_create_sample": True,
                "can_create_dataset": True,
                "can_create_for_others": True,
            },
        ),
        (
            Principal(
                user_id="service-account-admin",
                platform_role=PlatformRole.ADMIN,
                is_service_account=True,
            ),
            {
                "can_manage_service_accounts": True,
                "can_create_project": True,
                "can_register_instrument": False,
                "can_create_sample": True,
                "can_create_dataset": True,
                "can_create_for_others": True,
            },
        ),
    ],
)
def test_account_capabilities_match_route_policy(principal, expected):
    assert _account_capabilities(principal).model_dump() == expected


def test_account_profile_returns_capabilities(db_engine):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Upload",
            last_name="Service",
            email=None,
            unique_id="service-account-profile",
            username="profile-bot",
            is_service_account=True,
            platform_role=PlatformRole.CONTRIBUTOR.value,
        ))
        session.commit()
    principal = Principal(
        user_id="service-account-profile",
        platform_role=PlatformRole.CONTRIBUTOR,
        is_service_account=True,
    )

    response = get_account_profile(principal)

    assert response.capabilities.model_dump() == {
        "can_manage_service_accounts": False,
        "can_create_project": True,
        "can_register_instrument": False,
        "can_create_sample": True,
        "can_create_dataset": True,
        "can_create_for_others": True,
    }
