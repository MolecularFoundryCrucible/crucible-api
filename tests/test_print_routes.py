import app.api.routes.print as print_routes


def test_print_barcode_publishes_and_returns_job(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")
    captured = {}

    def fake_publish(printer_id, mfid, name):
        captured["args"] = (printer_id, mfid, name)
        return {"job_id": "test-job-id", "mfid": mfid, "name": name, "ts": 1700000000.0}

    monkeypatch.setattr(print_routes, "_publish_barcode_print_job", fake_publish)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body == {
        "job_id": "test-job-id",
        "printer_id": "lab3-zebra",
        "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz",
        "name": "Sample A",
        "ts": 1700000000.0,
    }
    assert captured["args"] == ("lab3-zebra", "01hzzzzzzzzzzzzzzzzzzzzzzz", "Sample A")
    assert response.headers["cache-control"] == "no-store"


def test_print_barcode_rejects_invalid_printer_id(client, auth_as, monkeypatch):
    auth_as("0000-0000-0000-0001")
    called = False

    def fake_publish(*args, **kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(print_routes, "_publish_barcode_print_job", fake_publish)

    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3/zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )

    assert response.status_code == 422
    assert called is False


def test_print_barcode_requires_authentication(client):
    response = client.post(
        "/print/barcode",
        json={"printer_id": "lab3-zebra", "mfid": "01hzzzzzzzzzzzzzzzzzzzzzzz", "name": "Sample A"},
    )
    assert response.status_code in (401, 403)
