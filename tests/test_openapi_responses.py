from app.api.main import app


def test_every_success_response_documents_its_content():
    untyped = []
    for path, operations in app.openapi()["paths"].items():
        for method, operation in operations.items():
            for code, response in operation.get("responses", {}).items():
                schema = response.get("content", {}).get("application/json", {}).get("schema")
                if code.startswith("2") and schema == {}:
                    untyped.append(f"{method.upper()} {path} {code}")
    assert untyped == []


def test_redirect_and_plain_text_routes_are_not_documented_as_json():
    paths = app.openapi()["paths"]
    for path in ("/auth/login", "/auth/callback", "/login", "/auth"):
        responses = paths[path]["get"]["responses"]
        assert "307" in responses and "200" not in responses, path
    assert list(paths["/metrics"]["get"]["responses"]["200"]["content"]) == ["text/plain"]
