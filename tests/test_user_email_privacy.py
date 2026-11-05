from fastapi import Request, Response
from sqlmodel import Session, select

from app.api.main import app
from app.api.routes.users import list_users, read_user
from app.models.core_models import User
from app.models.query_params import UserListQuery
from app.models.users import UserPublicRead, UserRead
from app.utils.permissions import PlatformRole, Principal


USER_ID = "0000-0001-2345-6789"


def _internal_user_id(db_engine):
    with Session(db_engine) as session:
        return session.exec(select(User.id).where(User.unique_id == USER_ID)).one()


def _seed_user(db_engine):
    with Session(db_engine) as session:
        session.add(User(
            first_name="Alice",
            last_name="Researcher",
            email="alice@example.com",
            unique_id=USER_ID,
            username="alice_researcher",
        ))
        session.commit()


def _request(query_string=b""):
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/users",
        "headers": [],
        "query_string": query_string,
    })


def _params(**overrides):
    return UserListQuery(**overrides)


def test_membership_and_project_schemas_do_not_expose_email():
    schema = app.openapi()
    components = schema["components"]["schemas"]

    assert "email" not in components["AccessGroupMemberRead"]["properties"]
    assert "email" not in components["UserPublicRead"]["properties"]
    assert (
        components["UserPublicRead"]["properties"]["is_service_account"]["type"]
        == "boolean"
    )
    assert components["ProjectWithLead"]["properties"]["lead"]["anyOf"][0] == {
        "$ref": "#/components/schemas/UserPublicRead"
    }


def test_exact_user_collection_and_singleton_share_authorized_representation_types():
    schema = app.openapi()

    collection_schema = schema["paths"]["/users"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    collection_component = schema["components"]["schemas"][collection_schema["$ref"].rsplit("/", 1)[-1]]
    collection_items = collection_component["properties"]["items"]["items"]["anyOf"]
    singleton_schema = schema["paths"]["/users/{user_ref}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    expected = {
        "#/components/schemas/UserRead",
        "#/components/schemas/UserPublicRead",
    }
    assert {item["$ref"] for item in collection_items} == expected
    assert {item["$ref"] for item in singleton_schema["anyOf"]} == {
        "#/components/schemas/UserRead",
        "#/components/schemas/UserPublicRead",
    }


def test_exact_collection_returns_private_self_and_public_other(db_engine):
    _seed_user(db_engine)

    self_result = list_users(
        auth_res=Principal(user_id=USER_ID, internal_user_id=_internal_user_id(db_engine)),
        request=_request(b"username=alice_researcher"),
        response=Response(),
        params=_params(username=["alice_researcher"]),
    )
    other_result = list_users(
        auth_res=Principal(user_id="0000-0002-3456-7890"),
        request=_request(b"username=alice_researcher"),
        response=Response(),
        params=_params(username=["alice_researcher"]),
    )

    assert isinstance(self_result.items[0], UserRead)
    assert self_result.items[0].email == "alice@example.com"
    assert isinstance(other_result.items[0], UserPublicRead)
    assert other_result.items[0].is_service_account is False


def test_public_profile_identifies_service_accounts():
    result = UserPublicRead.model_validate(
        User(
            first_name="Beamline",
            last_name="Collector",
            unique_id="0tkn2knjast3h0008nyq9zps2c",
            username="beamline-collector",
            is_service_account=True,
        )
    )

    assert result.is_service_account is True


def test_canonical_singleton_returns_public_profile_to_other_caller(db_engine):
    _seed_user(db_engine)

    result = read_user(
        user_ref=USER_ID,
        response=Response(),
        auth_res=Principal(user_id="0000-0002-3456-7890", platform_role=PlatformRole.NONE),
    )

    assert isinstance(result, UserPublicRead)


def test_broad_admin_directory_returns_private_fields(db_engine):
    _seed_user(db_engine)

    result = list_users(
        auth_res=Principal(user_id="admin", platform_role=PlatformRole.ADMIN),
        request=_request(),
        response=Response(),
        params=_params(),
    )

    assert isinstance(result.items[0], UserRead)
    assert result.items[0].email == "alice@example.com"
    assert result.items[0].user_capabilities.can_view_private is True
