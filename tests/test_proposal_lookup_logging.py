import logging
from unittest.mock import MagicMock

import pytest
import requests

import app.utils.user_utils as user_utils

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
    monkeypatch.setattr(user_utils, "get_secret", lambda *args: KEY)
    monkeypatch.setattr(user_utils.requests, "get", MagicMock(side_effect=error))
    with caplog.at_level(logging.DEBUG):
        assert user_utils.find_mf_user_proposals("0000-0001") == []
    assert KEY not in caplog.text
    assert f"MF proposal lookup failed for 0000-0001: {expected}" in caplog.text


def test_request_urls_are_not_logged_at_debug():
    assert logging.getLogger("urllib3.connectionpool").getEffectiveLevel() >= logging.WARNING
