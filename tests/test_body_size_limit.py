import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import config
from app.api import main
from app.api.body_limit import BodySizeLimit

MIB = 1024 * 1024


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setenv("API_MAX_BODY_MIB", "1")
    monkeypatch.setenv("API_MAX_THUMBNAIL_BODY_MIB", "2")
    config.get_settings.cache_clear()


@pytest.fixture
def echo(limits):
    app = FastAPI()
    parsed = []

    @app.middleware("http")
    async def reads_bodies(request: Request, call_next):
        parsed.append(len(await request.body()))
        return await call_next(request)

    @app.post("/{path:path}")
    @app.patch("/{path:path}")
    async def accept(request: Request):
        return {"size": len(await request.body())}

    app.add_middleware(BodySizeLimit)
    with TestClient(app) as client:
        client.parsed = parsed
        yield client


def test_a_body_over_the_limit_is_refused_before_anything_reads_it(echo):
    refused = echo.post("/datasets", content=b"x" * (MIB + 1), headers={"content-type": "application/json"})
    assert refused.status_code == 413 and refused.json()["detail"]["code"] == "body_too_large"
    assert echo.parsed == []
    assert echo.post("/datasets", content=b"x" * MIB).status_code == 200


def test_a_body_sent_without_a_length_is_counted_as_it_arrives(echo):
    def chunks():
        for _ in range(3):
            yield b"x" * (MIB // 2 + 1)

    refused = echo.post("/datasets", content=chunks())
    assert refused.status_code == 413


def test_thumbnail_writes_get_the_larger_limit(echo):
    body = b"x" * (MIB + MIB // 2)
    assert echo.post("/datasets/abc/thumbnails", content=body).status_code == 200
    assert echo.patch("/datasets/abc/thumbnails/7", content=body).status_code == 200
    assert echo.post("/api/v3/datasets/abc/thumbnails", content=body).status_code == 200
    assert echo.post("/datasets/abc/thumbnails", content=b"x" * (2 * MIB + 1)).status_code == 413
    assert echo.post("/datasets/abc", content=body).status_code == 413


def test_a_malformed_length_is_refused(echo):
    assert echo.post("/datasets", content=b"{}", headers={"content-length": "-5"}).status_code in (400, 413)


def test_the_api_refuses_an_oversized_body_before_authentication(limits):
    with TestClient(main.app) as client:
        refused = client.post("/no-such-route", content=b"{" + b" " * (MIB + 1) + b"}", headers={"content-type": "application/json"})
    assert refused.status_code == 413
