import re

import pytest

from app.config import get_settings
from app.models.response_schemas import FeaturesRead

SWITCHES = {
    "uploads": "MULTIPART_UPLOAD_ENABLED", "ingestion": "INGESTION_ENABLED", "printing": "PRINTING_ENABLED",
    "proposal_sync": "PROPOSAL_SYNC_ENABLED", "passwords": "CRUCIBLE_AUTH_PASSWORD_ENABLED",
}


def test_features_need_sign_in(client):
    assert client.get("/features").status_code in (401, 403)


@pytest.mark.parametrize("feature", sorted(SWITCHES))
def test_each_feature_follows_its_setting(client, auth_as, monkeypatch, feature):
    auth_as("0000-0002-1825-0097")
    for name, variable in SWITCHES.items():
        monkeypatch.setenv(variable, "true" if name == feature else "false")
    monkeypatch.setenv("MULTIPART_UPLOAD_BUCKET", "mf-storage-prod")
    monkeypatch.setenv("CRUCIBLE_AUTH_PEPPER", "p" * 32)
    get_settings.cache_clear()
    answer = client.get("/features")
    assert answer.status_code == 200, answer.text
    assert answer.json() == {name: name == feature for name in SWITCHES}


def test_every_feature_a_route_can_report_as_disabled_is_listed():
    from app.api.main import app

    named = set()
    for operations in app.openapi()["paths"].values():
        for operation in operations.values():
            description = operation.get("responses", {}).get("501", {}).get("description", "")
            named.update(re.findall(r'"feature": "(\w+)"', description))
    assert named and named <= set(FeaturesRead.model_fields)
