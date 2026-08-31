from sqlmodel import Session, select

from app.api.routes import users as users_routes
from app.models.core_models import AccessGroup, User, UserAccessGroupLink
from app.models.users import AccountProfileUpdate, UserCreate, UserUpdate
from app.utils.permissions import AccessGroupType, PlatformRole


def test_platform_admin_can_create_orcidless_human(client, db_engine, auth_as, monkeypatch):
    monkeypatch.setattr(users_routes, "find_or_add_api_access", lambda *args, **kwargs: "test-key")
    auth_as("platform-admin", platform_role=PlatformRole.ADMIN)

    response = client.post("/users", json={
        "user_info": {
            "first_name": "Test",
            "last_name": "User",
            "username": "test-user",
        },
        "project_ids": [],
    })

    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["unique_id"]) == 26
    assert body["is_service_account"] is False

    with Session(db_engine) as session:
        user = session.exec(select(User).where(User.unique_id == body["unique_id"])).one()
        group = session.exec(select(AccessGroup).where(
            AccessGroup.group_name == body["unique_id"],
            AccessGroup.group_type == AccessGroupType.IDENTITY.value,
        )).one()
        membership = session.exec(select(UserAccessGroupLink).where(
            UserAccessGroupLink.user_id == user.id,
            UserAccessGroupLink.accessgroup_id == group.id,
        )).one()
        assert membership.role == "owner"


def test_user_schemas_reject_service_account_mutation():
    for model, payload in (
        (UserCreate, {"first_name": "A", "last_name": "B", "username": "human-user", "is_service_account": True}),
        (UserUpdate, {"is_service_account": True}),
        (AccountProfileUpdate, {"is_service_account": True}),
    ):
        try:
            model.model_validate(payload)
        except ValueError:
            continue
        raise AssertionError(f"{model.__name__} accepted is_service_account")
