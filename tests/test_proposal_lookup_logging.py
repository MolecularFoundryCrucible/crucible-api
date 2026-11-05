import logging
from unittest.mock import MagicMock

import pytest
import requests

import app.services.proposals as proposals

KEY = "s3cr3t-proposal-key"
URL = f"https://foundry-admin.lbl.gov/api/JSON/sciCat-GetUser.aspx?key={KEY}&orcid=0000-0001"


def _http_error():
    response = MagicMock(status_code=503, url=URL)
    return requests.HTTPError(f"503 Server Error: Unavailable for url: {URL}", response=response)


@pytest.mark.parametrize("error, expected", [
    (_http_error(), "HTTPError 503"),
    (requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: {URL}"), "ConnectionError"),
    (requests.Timeout(f"Read timed out for {URL}"), "Timeout"),
])
def test_failures_are_logged_without_the_key(monkeypatch, caplog, error, expected):
    monkeypatch.setattr(proposals, "get_secret", lambda *args: KEY)
    monkeypatch.setattr(proposals.requests, "get", MagicMock(side_effect=error))
    with caplog.at_level(logging.DEBUG):
        assert proposals.find_mf_user_proposals("0000-0001") == []
    assert KEY not in caplog.text
    assert f"MF proposal lookup failed for 0000-0001: {expected}" in caplog.text


def test_request_urls_are_not_logged_at_debug():
    assert logging.getLogger("urllib3.connectionpool").getEffectiveLevel() >= logging.WARNING


def test_disabled_sync_makes_no_request(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("PROPOSAL_SYNC_ENABLED", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(proposals.requests, "get", MagicMock(side_effect=AssertionError("no request when disabled")))
    assert proposals.find_mf_user_proposals("0000-0001-0000-0001") == []


def test_lookup_uses_the_configured_url(monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("PROPOSAL_DB_URL", "https://proposals.lab.example/api")
    get_settings.cache_clear()
    monkeypatch.setattr(proposals, "get_secret", lambda *args: KEY)
    get = MagicMock(return_value=MagicMock(json=MagicMock(return_value=[])))
    monkeypatch.setattr(proposals.requests, "get", get)
    proposals.find_mf_user_proposals("0000-0001")
    assert get.call_args.args[0] == "https://proposals.lab.example/api"


def test_sync_route_disabled(client, auth_as, monkeypatch):
    from app.config import get_settings

    auth_as("0000-0001-0000-0001")
    monkeypatch.setenv("PROPOSAL_SYNC_ENABLED", "false")
    get_settings.cache_clear()
    response = client.post("/account/sync-projects")
    assert response.status_code == 501
    assert response.json()["detail"]["feature"] == "proposal_sync"
