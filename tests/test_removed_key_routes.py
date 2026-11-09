import pytest

ROUTES = (
    ("GET", "/account/apikey", "POST /account/apikeys"),
    ("POST", "/account/apikey/regenerate", "POST /account/apikeys"),
    ("POST", "/users/0tmnyhfztdwc1000w2kspzsnh0/apikey", "/users/{user_ref}/apikeys"),
    ("POST", "/users/0tmnyhfztdwc1000w2kspzsnh0/apikey/regenerate", "/users/{user_ref}/apikeys"),
)


@pytest.mark.parametrize("method,path,replacement", ROUTES)
def test_removed_key_routes_answer_gone_and_name_the_replacement(client, auth_as, method, path, replacement):
    for authenticated in (False, True):
        if authenticated:
            auth_as("0tmnyhfztdwc1000w2kspzsnh0")
        answer = client.request(method, path)
        assert answer.status_code == 410, answer.text
        assert answer.json()["detail"]["code"] == "route_removed" and replacement in answer.json()["detail"]["message"]
