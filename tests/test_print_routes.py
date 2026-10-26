import time

import app.api.routes.print as print_routes


def test_print_barcode_publishes_and_returns_ok(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")
    captured = {}

    def fake_publish(printer_id, mfid, name):
        captured["args"] = (printer_id, mfid, name)
        payload = {"job_id": "test-job-id", "mfid": mfid, "name": name, "ts": 1700000000.0}
        return payload, {"job_id": "test-job-id", "status": "ok"}, {"state": "online"}

    def fail_if_called(status):
        raise AssertionError("_printer_online should not be called on success")

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)
    monkeypatch.setattr(print_routes, "_printer_online", fail_if_called)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "job_id": "test-job-id",
        "printer_id": "lab3-zebra",
        "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz",
        "name": "Sample A",
        "ts": 1700000000.0,
        "status": "ok",
        "detail": None,
    }
    assert captured["args"] == ("lab3-zebra", "01hzzzzzzzzzzzzzzzzzzzzzzz", "Sample A")
    assert response.headers["cache-control"] == "no-store"


def test_print_barcode_reports_explicit_printer_error(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")

    def fake_publish(printer_id, mfid, name):
        payload = {"job_id": "test-job-id", "mfid": mfid, "name": name, "ts": 1700000000.0}
        return payload, {"job_id": "test-job-id", "status": "error"}, None

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "error"
    assert "not currently online" in body["detail"]


def test_print_barcode_reports_timeout_when_no_result(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")

    def fake_publish(printer_id, mfid, name):
        payload = {"job_id": "test-job-id", "mfid": mfid, "name": name, "ts": 1700000000.0}
        return payload, None, {"state": "online", "ts": time.time()}

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "timeout"
    assert body["detail"] is None


def test_print_barcode_timeout_detail_when_no_status_seen(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")

    def fake_publish(printer_id, mfid, name):
        payload = {"job_id": "test-job-id", "mfid": mfid, "name": name, "ts": 1700000000.0}
        return payload, None, None

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "timeout"
    assert "not currently online" in body["detail"]


def test_print_barcode_rejects_invalid_printer_id(client, auth_as, monkeypatch):
    called = False
    auth_as("0000-0000-0000-0001")

    def fake_publish(*args, **kwargs):
        nonlocal called
        called = True
        return {}, None, None

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3/zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 422
    assert called is False


def test_print_barcode_rejects_invalid_mfid(client, auth_as, monkeypatch):
    called = False
    auth_as("0000-0000-0000-0001")

    def fake_publish(*args, **kwargs):
        nonlocal called
        called = True
        return {}, None, None

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "not-a-real-mfid", "name": "Sample A"},
    )

    assert response.status_code == 422
    assert called is False


def test_print_barcode_requires_authentication(client):
    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )
    assert response.status_code in (401, 403)


def test_printer_online_true_for_fresh_heartbeat():
    assert print_routes._printer_online({"state": "online", "ts": time.time()}) is True


def test_printer_online_true_for_connect_status_without_ts():
    # The very first "online" status after connect has no ts -- only the
    # periodic heartbeat includes one.
    assert print_routes._printer_online({"state": "online"}) is True


def test_printer_online_false_for_stale_heartbeat():
    stale_ts = time.time() - print_routes.STATUS_STALE_THRESHOLD - 1
    assert print_routes._printer_online({"state": "online", "ts": stale_ts}) is False


def test_printer_online_false_for_explicit_offline():
    assert print_routes._printer_online({"state": "offline"}) is False


def test_printer_online_false_when_never_published():
    assert print_routes._printer_online(None) is False


def test_list_printers_returns_sorted_discovered_printers(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")

    def fake_list_statuses():
        return {
            "spinbot": {"state": "online", "hostname": "pi-spinbot", "ip": "10.0.0.5", "ts": time.time()},
            "ncem": {"state": "offline", "hostname": "pi-ncem", "ip": "10.0.0.6"},
        }

    monkeypatch.setattr(print_routes, "_list_printer_statuses", fake_list_statuses)

    response = client.get("/print/printers")

    assert response.status_code == 200
    body = response.json()
    assert [p["printer_id"] for p in body["printers"]] == ["ncem", "spinbot"]
    ncem, spinbot = body["printers"]
    assert ncem["online"] is False
    assert spinbot["online"] is True
    assert all(set(printer) == {"printer_id", "online", "ts"} for printer in body["printers"])


def test_list_printers_empty_when_none_discovered(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")
    monkeypatch.setattr(print_routes, "_list_printer_statuses", lambda: {})

    response = client.get("/print/printers")

    assert response.status_code == 200
    assert response.json() == {"printers": []}


def test_list_printers_requires_authentication(client):
    response = client.get("/print/printers")
    assert response.status_code in (401, 403)


def test_print_routes_disabled(client, auth_as, monkeypatch):
    from app.config import get_settings

    auth_as("0000-0000-0000-0001")
    monkeypatch.setenv("PRINTING_ENABLED", "false")
    get_settings.cache_clear()

    def fail(*args, **kwargs):
        raise AssertionError("no broker connection when printing is disabled")

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fail)
    monkeypatch.setattr(print_routes, "_list_printer_statuses", fail)
    listing = client.get("/print/printers")
    printing = client.post("/print/barcode", json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"})
    for response in (listing, printing):
        assert response.status_code == 503
        assert response.json()["detail"]["code"] == "feature_disabled"
        assert response.json()["detail"]["feature"] == "printing"
