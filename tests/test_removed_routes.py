import pytest

ROUTES = (
    ("GET", "/account/apikey", "POST /account/apikeys"),
    ("POST", "/account/apikey/regenerate", "POST /account/apikeys"),
    ("POST", "/users/0tmnyhfztdwc1000w2kspzsnh0/apikey", "/users/{user_ref}/apikeys"),
    ("POST", "/users/0tmnyhfztdwc1000w2kspzsnh0/apikey/regenerate", "/users/{user_ref}/apikeys"),
    ("POST", "/datasets/0tmnyhfztdwc1000w2kspzsnh1/files/0tmnyhfztdwc1000w2kspzsnh2/ingest", "POST /files/{file_id}/ingest"),
    ("GET", "/datasets/0tmnyhfztdwc1000w2kspzsnh1/samples", "GET /samples?dataset_mfid="),
    ("GET", "/users/by-username/ada", "GET /users/{user_ref}"),
    ("GET", "/users/0tmnyhfztdwc1000w2kspzsnh0/datasets", "GET /datasets?accessible_to_user="),
    ("GET", "/users/0tmnyhfztdwc1000w2kspzsnh0/samples", "GET /samples?accessible_to_user="),
    *((method, path, "POST /account/apikeys") for method, path in (
        ("GET", "/auth/login"), ("GET", "/auth/callback"), ("GET", "/auth/apikey"), ("POST", "/auth/apikey/regenerate"), ("GET", "/login"), ("GET", "/auth"), ("GET", "/user_apikey"))),
)


@pytest.mark.parametrize("method,path,replacement", ROUTES)
def test_removed_routes_answer_gone_and_name_the_replacement(client, auth_as, method, path, replacement):
    for authenticated in (False, True):
        if authenticated:
            auth_as("0tmnyhfztdwc1000w2kspzsnh0")
        answer = client.request(method, path)
        assert answer.status_code == 410, answer.text
        assert answer.json()["detail"]["code"] == "route_removed" and replacement in answer.json()["detail"]["message"]
