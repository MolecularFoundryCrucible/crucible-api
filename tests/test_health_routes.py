import json

from app.api.routes import misc
from app.build_info import api_version


def _response_body(response):
    return json.loads(response.body)


def test_readiness_reports_build_and_schema_provenance(monkeypatch):
    monkeypatch.delenv("EXPECTED_DB_REVISIONS", raising=False)
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
    monkeypatch.delenv("API_VERSION", raising=False)
    assert api_version() == "3.0.0"
