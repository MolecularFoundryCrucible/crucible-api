from app.api.main import app


def test_membership_and_project_schemas_do_not_expose_email():
    schema = app.openapi()
    components = schema["components"]["schemas"]

    assert "email" not in components["AccessGroupMemberRead"]["properties"]
    assert "email" not in components["UserPublicRead"]["properties"]
    assert components["ProjectWithLead"]["properties"]["lead"]["anyOf"][0] == {
        "$ref": "#/components/schemas/UserPublicRead"
    }


def test_user_collection_is_public_safe_but_private_singletons_remain_available():
    schema = app.openapi()

    assert schema["paths"]["/users"]["get"]["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/PaginatedResponse_UserPublicRead_"
    }
    singleton_schema = schema["paths"]["/users/{user_id}"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert {item["$ref"] for item in singleton_schema["anyOf"]} == {
        "#/components/schemas/UserRead",
        "#/components/schemas/UserPublicRead",
    }
