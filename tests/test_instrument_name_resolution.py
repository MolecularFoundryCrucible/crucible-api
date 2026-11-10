"""instrument_name is display text resolved through instrument_mfid, never an identifier."""
from sqlmodel import Session, select

from app.tables import AccessGroup, Dataset, Resource
from app.utils.permissions import PlatformRole
from tests.test_instrument_lifecycle import _user

ADMIN = "admin-1"


def _register(client, db_engine, auth_as, instrument_id="xrd-1", name="XRD One"):
    with Session(db_engine) as session:
        if session.exec(select(AccessGroup).where(AccessGroup.group_name == ADMIN)).first() is None:
            _user(session, ADMIN)
    auth_as(ADMIN, groups=[], platform_role=PlatformRole.ADMIN)
    resp = client.post("/instruments", json={"instrument_id": instrument_id, "instrument_name": name, "location": "b30"})
    assert resp.status_code == 200, resp.text
    return resp.json()["unique_id"]


def _rename(client, mfid, name):
    assert client.patch(f"/instruments/{mfid}", json={"instrument_name": name}).status_code == 200


def _total(client, **params):
    return client.get("/datasets", params={"include_total": True, **params}).json()["total"]


class TestDisplayNameFollowsTheInstrument:
    def test_dataset_reads_show_the_current_name(self, client, db_engine, auth_as):
        mfid = _register(client, db_engine, auth_as)
        dsid = client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-1"}).json()["unique_id"]
        _rename(client, mfid, "XRD Two")

        detail = client.get(f"/datasets/{dsid}").json()
        assert detail["instrument_name"] == "XRD Two"
        assert detail["instrument"]["instrument_name"] == "XRD Two"
        assert client.get("/datasets").json()["items"][0]["instrument_name"] == "XRD Two"
        assert client.get(f"/resources/{dsid}").json()["instrument_name"] == "XRD Two"

    def test_patch_response_shows_the_current_name(self, client, db_engine, auth_as):
        mfid = _register(client, db_engine, auth_as)
        dsid = client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-1"}).json()["unique_id"]
        _rename(client, mfid, "XRD Two")
        resp = client.patch(f"/datasets/{dsid}", json={"measurement": "m"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument_name"] == "XRD Two"

    def test_dataset_without_a_registered_instrument_shows_none(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as)
        with Session(db_engine) as session:
            session.add(Dataset(unique_id="0legacy0000000000000000000", dataset_name="old"))
            session.add(Resource(mfid="0legacy0000000000000000000", resource_type="dataset"))
            session.commit()
        body = client.get("/datasets/0legacy0000000000000000000").json()
        assert (body["instrument_name"], body["instrument_id"], body["instrument"]) == (None, None, None)

    def test_patch_accepts_only_the_current_name(self, client, db_engine, auth_as):
        mfid = _register(client, db_engine, auth_as)
        dsid = client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-1"}).json()["unique_id"]
        _rename(client, mfid, "XRD Two")
        assert client.patch(f"/datasets/{dsid}", json={"instrument_name": "xrd two"}).status_code == 200
        assert client.patch(f"/datasets/{dsid}", json={"instrument_name": "XRD One"}).status_code == 409
        assert client.patch(f"/datasets/{dsid}", json={"instrument_name": "Other"}).status_code == 409


class TestDeprecatedNameFilter:
    def test_filter_matches_the_current_name(self, client, db_engine, auth_as):
        mfid = _register(client, db_engine, auth_as)
        client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-1"})
        _rename(client, mfid, "XRD Two")
        assert _total(client, instrument_name="XRD Two") == 1
        assert _total(client, instrument_name="XRD One") == 0

    def test_filter_matches_only_registered_instruments(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as)
        with Session(db_engine) as session:
            session.add(Dataset(unique_id="0legacy0000000000000000000", dataset_name="old"))
            session.add(Resource(mfid="0legacy0000000000000000000", resource_type="dataset"))
            session.commit()
        assert _total(client, instrument_name="XRD One") == 0

    def test_filter_is_deprecated_in_openapi(self, client):
        parameters = client.get("/openapi.json").json()["paths"]["/datasets"]["get"]["parameters"]
        assert next(p for p in parameters if p["name"] == "instrument_name")["deprecated"] is True


class TestDeprecatedNameFallbackOnCreate:
    def test_unique_name_still_resolves(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as)
        resp = client.post("/datasets", json={"dataset_name": "d", "instrument_name": "xrd one"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument_id"] == "xrd-1"
        assert resp.json()["instrument_name"] == "XRD One"

    def test_ambiguous_name_is_a_conflict_not_a_server_error(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as, "xrd-1", "Shared")
        _register(client, db_engine, auth_as, "xrd-2", "shared")
        resp = client.post("/datasets", json={"dataset_name": "d", "instrument_name": "Shared"})
        assert resp.status_code == 409
        assert "instrument_id" in resp.json()["detail"]

    def test_slug_wins_over_an_ambiguous_name(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as, "xrd-1", "Shared")
        _register(client, db_engine, auth_as, "xrd-2", "Shared")
        resp = client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-2", "instrument_name": "Shared"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument_id"] == "xrd-2"

    def test_create_field_is_deprecated_in_openapi(self, client):
        schema = client.get("/openapi.json").json()["components"]["schemas"]["DatasetCreate"]
        assert schema["properties"]["instrument_name"]["deprecated"] is True


def test_status_change_writes_an_audit_record(client, db_engine, auth_as, monkeypatch):
    import app.api.routes.instruments as instruments_routes
    writes = []
    monkeypatch.setattr(instruments_routes, "write_record", lambda *args: writes.append(args))
    mfid = _register(client, db_engine, auth_as)
    writes.clear()
    assert client.post(f"/instruments/{mfid}/status", params={"status": "maintenance"}).status_code == 200
    (written_mfid, record, actor, operation), = writes
    assert (written_mfid, actor, operation) == (mfid, ADMIN, "update")
    assert record["status"] == "maintenance"
    assert record["instrument_id"] == "xrd-1"
    assert record["location"] == "b30"
