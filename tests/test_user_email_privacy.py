from fastapi import Request, Response
from sqlmodel import Session

from app.api.main import app
from app.api.routes.users import list_users, read_user
from app.models.core_models import User
from app.models.users import UserPublicRead, UserRead
from app.utils.permissions import PlatformRole, Principal


USER_ID = "0000-0001-2345-6789"


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


def test_membership_and_project_schemas_do_not_expose_email():
    schema = app.openapi()
    components = schema["components"]["schemas"]

    assert "email" not in components["AccessGroupMemberRead"]["properties"]
    assert "email" not in components["UserPublicRead"]["properties"]
    assert components["ProjectWithLead"]["properties"]["lead"]["anyOf"][0] == {
        "$ref": "#/components/schemas/UserPublicRead"
    }


def test_exact_user_collection_and_singleton_share_authorized_representation_types():
    schema = app.openapi()

    collection_schema = schema["paths"]["/users"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    collection_component = schema["components"]["schemas"][collection_schema["$ref"].rsplit("/", 1)[-1]]
    collection_items = collection_component["properties"]["items"]["items"]["anyOf"]
    singleton_schema = schema["paths"]["/users/{user_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
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
        auth_res=Principal(user_id=USER_ID),
        request=_request(b"username=alice_researcher"),
        unique_id=None,
        username="alice_researcher",
        email=None,
        permissive=True,
        is_service_account=None,
        limit=100,
        offset=0,
    )
    other_result = list_users(
        auth_res=Principal(user_id="0000-0002-3456-7890"),
        request=_request(b"username=alice_researcher"),
        unique_id=None,
        username="alice_researcher",
        email=None,
        permissive=True,
        is_service_account=None,
        limit=100,
        offset=0,
    )

    assert isinstance(self_result.items[0], UserRead)
    assert self_result.items[0].email == "alice@example.com"
    assert isinstance(other_result.items[0], UserPublicRead)


def test_canonical_singleton_returns_public_profile_to_other_caller(db_engine):
    _seed_user(db_engine)

    result = read_user(
        user_id=USER_ID,
        response=Response(),
        auth_res=Principal(user_id="0000-0002-3456-7890", platform_role=PlatformRole.NONE),
    )

    assert isinstance(result, UserPublicRead)


def test_broad_admin_directory_remains_public_safe(db_engine):
    _seed_user(db_engine)

    result = list_users(
        auth_res=Principal(user_id="admin", platform_role=PlatformRole.ADMIN),
        request=_request(),
        unique_id=None,
        username=None,
        email=None,
        permissive=True,
        is_service_account=None,
        limit=100,
        offset=0,
    )

    assert isinstance(result.items[0], UserPublicRead)
