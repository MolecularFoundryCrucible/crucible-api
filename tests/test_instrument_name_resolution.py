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

    def test_patch_refuses_a_name_and_drops_a_null(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as)
        dsid = client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-1"}).json()["unique_id"]
        resp = client.patch(f"/datasets/{dsid}", json={"instrument_name": "XRD One"})
        assert resp.status_code == 422
        assert "instrument_id" in resp.text
        assert client.patch(f"/datasets/{dsid}", json={"instrument_name": None, "measurement": "m"}).status_code == 200


class TestNameIsNotAnInput:
    def test_create_refuses_a_name(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as)
        resp = client.post("/datasets", json={"dataset_name": "d", "instrument_name": "XRD One"})
        assert resp.status_code == 422
        assert "instrument_mfid" in resp.text
        resp = client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-1", "instrument_name": "XRD One"})
        assert resp.status_code == 422

    def test_create_drops_a_null_name(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as)
        resp = client.post("/datasets", json={"dataset_name": "d", "instrument_id": "xrd-1", "instrument_name": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["instrument_name"] == "XRD One"

    def test_list_refuses_the_name_filter(self, client, db_engine, auth_as):
        _register(client, db_engine, auth_as)
        resp = client.get("/datasets", params={"instrument_name": "XRD One"})
        assert resp.status_code == 422
        assert "instrument_mfid" in resp.text

    def test_name_is_not_in_the_request_schemas(self, client):
        openapi = client.get("/openapi.json").json()
        assert "instrument_name" not in openapi["components"]["schemas"]["DatasetCreate"]["properties"]
        assert "instrument_name" not in openapi["components"]["schemas"]["DatasetUpdate"]["properties"]
        assert "instrument_name" not in {p["name"] for p in openapi["paths"]["/datasets"]["get"]["parameters"]}


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
