import pathlib
import json

from app.api.routes import misc
from app.build_info import api_version


def _response_body(response):
    return json.loads(response.body)


def test_readiness_reports_build_and_schema_provenance(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", "head_a,head_b")
    monkeypatch.setattr(misc, "_check_db", lambda: ("ok", 2.5, ["head_a", "head_b"]))
    monkeypatch.setattr(misc, "api_version", lambda: "3.0.0")
    monkeypatch.setattr(misc, "git_commit", lambda: "abc123")
    monkeypatch.setattr(misc, "branch_name", lambda: "staging")

    response = misc.readiness()

    assert response.status_code == 200
    assert _response_body(response) == {
        "status": "ok",
        "build": {
            "api_version": "3.0.0",
            "git_commit": "abc123",
            "branch": "staging",
        },
        "database": {
            "status": "ok",
            "latency_ms": 2.5,
            "schema_revisions": ["head_a", "head_b"],
            "schema_state": "current",
        },
    }


def test_readiness_reports_database_failure(monkeypatch):
    monkeypatch.delenv("EXPECTED_DB_REVISIONS", raising=False)
    monkeypatch.setattr(misc, "_check_db", lambda: ("error", None, []))
    monkeypatch.setattr(misc, "api_version", lambda: "3.0.0")
    monkeypatch.setattr(misc, "git_commit", lambda: None)
    monkeypatch.setattr(misc, "branch_name", lambda: None)

    response = misc.readiness()

    assert response.status_code == 503
    assert _response_body(response) == {
        "status": "degraded",
        "build": {
            "api_version": "3.0.0",
            "git_commit": None,
            "branch": None,
        },
        "database": {
            "status": "error",
            "latency_ms": None,
            "schema_revisions": [],
            "schema_state": None,
        },
    }


def test_readiness_rejects_unexpected_schema_revision(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", "head_b")
    monkeypatch.setattr(misc, "_check_db", lambda: ("ok", 2.5, ["head_a"]))

    response = misc.readiness()

    assert response.status_code == 503
    assert _response_body(response)["status"] == "degraded"
    assert _response_body(response)["database"] == {
        "status": "error",
        "latency_ms": 2.5,
        "schema_revisions": ["head_a"],
        "schema_state": "mismatch",
    }


def test_readiness_accepts_expected_schema_revision(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", "head_b, head_a")
    monkeypatch.setattr(misc, "_check_db", lambda: ("ok", 2.5, ["head_a", "head_b"]))

    response = misc.readiness()

    assert response.status_code == 200
    assert _response_body(response)["database"]["status"] == "ok"


def test_database_check_sorts_multiple_schema_heads(monkeypatch):
    class Result:
        def scalars(self):
            return self

        def all(self):
            return ["head_b", "head_a"]

    class TestSession:
        def __init__(self, engine):
            self.engine = engine

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

        def execute(self, statement):
            assert "alembic_version" in str(statement)
            return Result()

    monkeypatch.setattr(misc, "Session", TestSession)

    status, latency_ms, revisions = misc._check_db()

    assert status == "ok"
    assert latency_ms is not None
    assert revisions == ["head_a", "head_b"]


def test_liveness_remains_minimal():
    assert misc.liveness() == {"status": "ok"}


def test_api_version_uses_project_metadata(monkeypatch):
    import tomllib

    monkeypatch.delenv("API_VERSION", raising=False)
    declared = tomllib.loads((pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text())["project"]["version"]
    assert api_version() == declared


HEAD = misc._migration_scripts().get_current_head()
PREVIOUS = misc._migration_scripts().get_revision(HEAD).down_revision


def test_readiness_stays_ready_when_the_database_is_ahead(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", HEAD)
    monkeypatch.setattr(misc, "_check_db", lambda: ("ok", 2.5, ["fffffffffff0"]))
    response = misc.readiness()
    assert response.status_code == 200
    assert _response_body(response)["database"]["schema_state"] == "ahead"


def test_readiness_fails_when_the_database_is_behind(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", HEAD)
    monkeypatch.setattr(misc, "_check_db", lambda: ("ok", 2.5, [PREVIOUS]))
    response = misc.readiness()
    assert response.status_code == 503
    assert _response_body(response)["database"]["schema_state"] == "behind"


def test_readiness_reports_current_schema(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", HEAD)
    monkeypatch.setattr(misc, "_check_db", lambda: ("ok", 2.5, [HEAD]))
    response = misc.readiness()
    assert response.status_code == 200
    assert _response_body(response)["database"]["schema_state"] == "current"


def test_readiness_rejects_an_unknown_revision_when_not_expecting_this_builds_head(monkeypatch):
    monkeypatch.setenv("EXPECTED_DB_REVISIONS", PREVIOUS)
    monkeypatch.setattr(misc, "_check_db", lambda: ("ok", 2.5, ["fffffffffff0"]))
    response = misc.readiness()
    assert response.status_code == 503
    assert _response_body(response)["database"]["schema_state"] == "mismatch"


def test_probe_and_scrape_requests_are_not_logged_when_successful(client, caplog):
    with caplog.at_level("INFO", logger="app.api.main"):
        client.get("/health/live")
        client.get("/metrics")
        client.get("/openapi.json")
    lines = [record.getMessage() for record in caplog.records if record.name == "app.api.main"]
    assert not any(line.startswith(("GET /health/live ", "GET /metrics ")) for line in lines)
    assert any(line.startswith("GET /openapi.json ") for line in lines)


def test_probe_failures_are_still_logged(client, caplog, monkeypatch):
    monkeypatch.setattr(misc, "_check_db", lambda: ("error", None, []))
    with caplog.at_level("INFO", logger="app.api.main"):
        client.get("/health/ready")
    assert any(record.getMessage().startswith("GET /health/ready 503 ") for record in caplog.records if record.name == "app.api.main")


def test_probes_are_excluded_from_request_metrics(client):
    client.get("/health/live")
    client.get("/health/ready")
    body = client.get("/metrics").text
    assert 'handler="/health/live"' not in body
    assert 'handler="/health/ready"' not in body
    assert 'handler="/metrics"' not in body
