import pytest
from fastapi import HTTPException
from sqlmodel import Session

from app.api.routes.service_accounts import get_service_account, list_service_accounts
from app.models.core_models import ApiToken, User
from app.utils.permissions import PlatformRole, Principal


def _seed(db_engine):
    with Session(db_engine) as session:
        session.add_all([
            User(
                first_name="",
                last_name="",
                email=None,
                unique_id="service-account-one",
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
            ApiToken(
                access_group_id="service-account-one",
                user_unique_id="service-account-one",
                token="secret-not-returned",
                token_created="2026-09-16",
            ),
        ])
        session.commit()


def test_admin_list_and_detail_expose_role_without_secret(db_engine):
    _seed(db_engine)
    admin = Principal(user_id="admin", platform_role=PlatformRole.ADMIN)

    listed = list_service_accounts(admin, q="directory", limit=100, offset=0)
    assert listed.total == 1
    assert len(listed.items) == 1
    assert listed.items[0].unique_id == "service-account-one"
    assert listed.items[0].platform_role is PlatformRole.CONTRIBUTOR
    assert not hasattr(listed.items[0], "api_key")

    detail = get_service_account("service-account-one", admin)
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
        get_service_account("service-account-one", ordinary)
    assert detail_error.value.status_code == 403
