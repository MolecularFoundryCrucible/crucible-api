import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, select

from app.api.routes.service_accounts import (
    get_service_account,
    list_service_accounts,
    update_service_account_platform_role,
)
from app.models.core_models import ApiToken, User
from app.models.users import ServiceAccountPlatformRoleUpdate
from app.utils.permissions import PlatformRole, Principal
from tests.people import internal_id


def _seed(db_engine):
    with Session(db_engine) as session:
        session.add_all([
            User(
                first_name="",
                last_name="",
                email=None,
                unique_id="0tp5sa0ne00000000000000001",
                username="directory-bot",
                is_service_account=True,
                platform_role=PlatformRole.CONTRIBUTOR.value,
            ),
            User(
                first_name="Human",
                last_name="User",
                email="human@example.com",
                unique_id="human-one",
                username="directory-human",
                is_service_account=False,
                platform_role=PlatformRole.NONE.value,
            ),
        ])
        session.add(ApiToken(
            access_group_id="0tp5sa0ne00000000000000001",
            user_id=internal_id(session, "0tp5sa0ne00000000000000001"),
            user_unique_id="0tp5sa0ne00000000000000001",
            token="secret-not-returned",
            token_created="2026-09-16",
        ))
        session.commit()


def test_admin_list_and_detail_expose_role_without_secret(db_engine):
    _seed(db_engine)
    admin = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    listed = list_service_accounts(admin, q="directory", limit=100, offset=0)
    assert listed.total == 1
    assert len(listed.items) == 1
    assert listed.items[0].unique_id == "0tp5sa0ne00000000000000001"
    assert listed.items[0].platform_role is PlatformRole.CONTRIBUTOR
    assert not hasattr(listed.items[0], "api_key")

    detail = get_service_account("0tp5sa0ne00000000000000001", admin)
    assert detail.platform_role is PlatformRole.CONTRIBUTOR
    assert detail.api_key_status is not None
    assert detail.api_key_status.valid is True
    assert not hasattr(detail, "api_key")


def test_service_account_admin_routes_require_platform_admin(db_engine):
    _seed(db_engine)
    ordinary = Principal(user_id="ordinary")

    with pytest.raises(HTTPException) as list_error:
        list_service_accounts(ordinary, q=None, limit=100, offset=0)
    assert list_error.value.status_code == 403

    with pytest.raises(HTTPException) as detail_error:
        get_service_account("0tp5sa0ne00000000000000001", ordinary)
    assert detail_error.value.status_code == 403

    with pytest.raises(HTTPException) as update_error:
        update_service_account_platform_role(
            "0tp5sa0ne00000000000000001",
            ServiceAccountPlatformRoleUpdate(platform_role="contributor"),
            ordinary,
        )
    assert update_error.value.status_code == 403


@pytest.mark.parametrize("role", ["none", "contributor", "admin"])
def test_human_platform_admin_updates_service_account_role(db_engine, role):
    _seed(db_engine)
    admin = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    result = update_service_account_platform_role(
        "0tp5sa0ne00000000000000001",
        ServiceAccountPlatformRoleUpdate(platform_role=role),
        admin,
    )

    assert result.platform_role.value == role
    assert result.api_key_status is not None
    assert not hasattr(result, "api_key")
    with Session(db_engine) as session:
        user = session.exec(
            select(User).where(User.unique_id == "0tp5sa0ne00000000000000001")
        ).one()
        assert user.platform_role == role


def test_admin_service_account_cannot_update_platform_roles(db_engine):
    _seed(db_engine)
    admin_service_account = Principal(
        user_id="service-account-admin",
        platform_role=PlatformRole.ADMIN,
        is_service_account=True,
    )

    with pytest.raises(HTTPException) as exc:
        update_service_account_platform_role(
            "0tp5sa0ne00000000000000001",
            ServiceAccountPlatformRoleUpdate(platform_role="admin"),
            admin_service_account,
        )

    assert exc.value.status_code == 403


def test_platform_role_update_rejects_reserved_support_role():
    with pytest.raises(ValidationError):
        ServiceAccountPlatformRoleUpdate(platform_role="support")


def test_platform_role_update_rejects_human_target(db_engine):
    _seed(db_engine)
    admin = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    with pytest.raises(HTTPException) as exc:
        update_service_account_platform_role(
            "human-one",
            ServiceAccountPlatformRoleUpdate(platform_role="admin"),
            admin,
        )

    assert exc.value.status_code == 404
