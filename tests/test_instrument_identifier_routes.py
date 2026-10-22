from sqlmodel import Session

from app.models.core_models import Instrument, Resource


def _setup(db_engine, status="maintenance"):
    with Session(db_engine) as session:
        instrument = Instrument(
            instrument_id="XRD_1",
            instrument_name="XRD instrument",
            owner="LBNL",
            location="Building 2",
            status=status,
        )
        session.add(instrument)
        session.commit()
        session.refresh(instrument)
        session.add(Resource(mfid=instrument.unique_id, resource_type="instrument"))
        session.commit()
        return instrument.unique_id


def test_canonical_get_uses_instrument_mfid(client, db_engine, auth_as):
    instrument_mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111")

    response = client.get(f"/instruments/{instrument_mfid}")

    assert response.status_code == 200, response.text
    assert "content-location" not in response.headers
    assert response.json()["unique_id"] == instrument_mfid
    assert response.json()["instrument_id"] == "XRD_1"
    assert response.json()["capabilities"] == {
        "can_edit": False,
        "can_manage_access": False,
        "can_view_access": False,
        "can_change_status": False,
        "can_transfer": False,
        "can_delete": False,
        "can_request_deletion": False,
        "max_grant_role": None,
    }


def test_single_instrument_get_accepts_slug_case_insensitively(client, db_engine, auth_as):
    instrument_mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111")

    response = client.get("/instruments/xrd_1")

    assert response.status_code == 200, response.text
    assert response.json()["unique_id"] == instrument_mfid
    assert response.headers["content-location"] == f"/instruments/{instrument_mfid}"


def test_single_instrument_get_unknown_slug_is_404(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001-1111-1111")

    assert client.get("/instruments/XRD_2").status_code == 404


def test_exact_slug_query_is_case_insensitive(client, db_engine, auth_as):
    instrument_mfid = _setup(db_engine)
    auth_as("0000-0001-1111-1111")

    response = client.get("/instruments", params={"instrument_id": "xrd_1"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["unique_id"] == instrument_mfid
    assert body["items"][0]["capabilities"]["can_edit"] is False


def test_exact_slug_query_includes_non_active_instrument(client, db_engine, auth_as):
    _setup(db_engine, status="decommissioned")
    auth_as("0000-0001-1111-1111")

    exact_response = client.get("/instruments", params={"instrument_id": "XRD_1"})
    default_response = client.get("/instruments")

    assert exact_response.status_code == 200, exact_response.text
    assert exact_response.json()["total"] == 1
    assert default_response.status_code == 200, default_response.text
    assert default_response.json()["total"] == 0


def test_exact_slug_query_honors_explicit_status_filter(client, db_engine, auth_as):
    _setup(db_engine, status="maintenance")
    auth_as("0000-0001-1111-1111")

    response = client.get(
        "/instruments",
        params={"instrument_id": "XRD_1", "status": "decommissioned"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


def test_exact_slug_query_returns_empty_collection_when_missing(client, db_engine, auth_as):
    _setup(db_engine)
    auth_as("0000-0001-1111-1111")

    response = client.get("/instruments", params={"instrument_id": "missing"})

    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0
    assert response.json()["items"] == []
