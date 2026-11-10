from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlmodel import Session, select

from app.api.routes.service_accounts import (
    get_service_account,
    list_service_accounts,
    update_service_account_platform_role,
)
from app.auth.events import AuthEventType
from app.tables import AuthEvent, User
from app.models.users import ServiceAccountPlatformRoleUpdate
from app.utils.permissions import PlatformRole, Principal
from tests.people import internal_id, add_old_style_key


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
        add_old_style_key(session, internal_id(session, "0tp5sa0ne00000000000000001"), "secret-not-returned", "2026-09-16")
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


def _fresh_session_admin(db_engine, **changes):
    with Session(db_engine) as session, session.begin():
        session.add(User(first_name="Ada", last_name="Admin", unique_id="admin", platform_role=PlatformRole.ADMIN.value))
    values = dict(user_id="admin", platform_role=PlatformRole.ADMIN, credential="session", authenticated_at=datetime.now(timezone.utc), internal_user_id=internal_id(db_engine, "admin"))
    return Principal(**{**values, **changes})


def _role_events(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(AuthEvent.user_id, AuthEvent.actor_user_id).where(AuthEvent.event == AuthEventType.ROLE_CHANGED.value)).all()


@pytest.mark.parametrize("changes, status, code", [
    ({"credential": "api_key"}, 403, "hub_session_required"),
    ({"authenticated_at": datetime.now(timezone.utc) - timedelta(days=1)}, 401, "insufficient_user_authentication"),
    ({"via_device": True}, 403, "browser_sign_in_required"),
])
def test_making_a_service_account_an_administrator_needs_a_recent_browser_sign_in(db_engine, changes, status, code):
    _seed(db_engine)
    with pytest.raises(HTTPException) as refused:
        update_service_account_platform_role("0tp5sa0ne00000000000000001", ServiceAccountPlatformRoleUpdate(platform_role="admin"), _fresh_session_admin(db_engine, **changes))
    assert refused.value.status_code == status and refused.value.detail["code"] == code
    with Session(db_engine) as session:
        assert session.exec(select(User.platform_role).where(User.unique_id == "0tp5sa0ne00000000000000001")).one() == "contributor"
    assert _role_events(db_engine) == []


def test_a_role_change_is_recorded_and_drops_the_cached_keys(db_engine, monkeypatch):
    _seed(db_engine)
    forgotten = []
    monkeypatch.setattr("app.api.routes.service_accounts.forget_cached_keys", lambda session, user_id: forgotten.append(user_id))
    admin = _fresh_session_admin(db_engine)
    target = internal_id(db_engine, "0tp5sa0ne00000000000000001")
    update_service_account_platform_role("0tp5sa0ne00000000000000001", ServiceAccountPlatformRoleUpdate(platform_role="admin"), admin)
    update_service_account_platform_role("0tp5sa0ne00000000000000001", ServiceAccountPlatformRoleUpdate(platform_role="admin"), admin)
    assert forgotten == [target]
    assert _role_events(db_engine) == [(target, admin.internal_user_id)]
    update_service_account_platform_role("0tp5sa0ne00000000000000001", ServiceAccountPlatformRoleUpdate(platform_role="none"), Principal(user_id="admin", platform_role=PlatformRole.ADMIN))
    assert forgotten == [target, target] and len(_role_events(db_engine)) == 2


@pytest.mark.parametrize("role", ["none", "contributor", "admin"])
def test_human_platform_admin_updates_service_account_role(db_engine, role):
    _seed(db_engine)
    admin = _fresh_session_admin(db_engine)

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
