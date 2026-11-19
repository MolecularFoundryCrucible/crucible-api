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


def test_metrics_are_documented_as_plain_text():
    paths = app.openapi()["paths"]
    assert list(paths["/metrics"]["get"]["responses"]["200"]["content"]) == ["text/plain"]
