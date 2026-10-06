import app.api.routes.print as print_routes


def test_print_barcode_publishes_and_returns_ok(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")
    captured = {}

    def fake_publish(printer_id, mfid, name):
        captured["args"] = (printer_id, mfid, name)
        payload = {"job_id": "test-job-id", "mfid": mfid, "name": name, "ts": 1700000000.0}
        return payload, {"job_id": "test-job-id", "status": "ok"}

    def fail_if_called(printer_id):
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
        return payload, {"job_id": "test-job-id", "status": "error"}

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)
    monkeypatch.setattr(print_routes, "_printer_online", lambda printer_id: False)

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
        return payload, None

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)
    monkeypatch.setattr(print_routes, "_printer_online", lambda printer_id: True)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "timeout"
    assert body["detail"] is None


def test_print_barcode_timeout_detail_swallows_status_check_failure(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")

    def fake_publish(printer_id, mfid, name):
        payload = {"job_id": "test-job-id", "mfid": mfid, "name": name, "ts": 1700000000.0}
        return payload, None

    monkeypatch.setattr(print_routes, "_publish_and_await_result", fake_publish)
    monkeypatch.setattr(print_routes, "_printer_online", lambda printer_id: None)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "timeout"
    assert body["detail"] is None


def test_print_barcode_rejects_invalid_printer_id(client, auth_as, monkeypatch):
    called = False
    auth_as("0000-0000-0000-0001")

    def fake_publish(*args, **kwargs):
        nonlocal called
        called = True
        return {}, None

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
        return {}, None

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


def test_printer_online_returns_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(print_routes, "label_printer_status_url", None)
    assert print_routes._printer_online("lab3-zebra") is None


def test_printer_online_swallows_request_errors(monkeypatch):
    import requests

    monkeypatch.setattr(print_routes, "label_printer_status_url", "https://status.example")

    def fake_get(*args, **kwargs):
        raise requests.RequestException("boom")

    monkeypatch.setattr(print_routes.requests, "get", fake_get)
    assert print_routes._printer_online("lab3-zebra") is None
