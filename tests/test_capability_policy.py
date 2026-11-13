"""Capabilities come from one policy per record type and match what the routes accept."""
from tests.test_instrument_members import ADMIN, OWNER, _as, _world


def test_instrument_capabilities_follow_membership(client, db_engine, auth_as):
    mfid, _ = _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, OWNER)
    owner = client.get(f"/instruments/{mfid}").json()["capabilities"]
    assert owner["can_delete"] is False
    assert owner["max_grant_role"] == "admin"
    _as(auth_as, db_engine, ADMIN)
    admin = client.get(f"/instruments/{mfid}").json()["capabilities"]
    assert admin["can_transfer"] is False and admin["can_delete"] is False
    assert admin["can_manage_access"] is True and admin["can_change_status"] is True
    assert client.delete(f"/resources/{mfid}", params={"force": True}).status_code == 403


def test_owners_may_delete_their_datasets_and_samples(client, db_engine, auth_as):
    _world(client, db_engine, auth_as)
    _as(auth_as, db_engine, OWNER)
    dataset = client.post("/datasets", json={"dataset_name": "mine"}).json()["unique_id"]
    sample = client.post("/samples", json={"sample_name": "mine"}).json()["unique_id"]
    for path, mfid in (("/datasets", dataset), ("/samples", sample)):
        capabilities = client.get(f"{path}/{mfid}").json()["capabilities"]
        assert capabilities["can_delete"] is True
        assert client.delete(f"/resources/{mfid}", params={"force": True}).status_code == 200


def test_capability_fields_are_documented(client):
    properties = client.get("/openapi.json").json()["components"]["schemas"]["ResourceCapabilities"]["properties"]
    assert all(field.get("description") for field in properties.values())
