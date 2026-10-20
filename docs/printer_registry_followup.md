# Printer registry: follow-up

Status: proposed 2026-10-20, not scheduled. `GET /print/printers` (PR #337) stays as implemented until then.

## Today

`GET /print/printers` opens an MQTT connection on every request, subscribes to `crucible-printer/+/status`, waits up to about 0.65 s for the broker to replay retained statuses, and returns `printer_id`, `online`, and `ts`. `POST /print/barcode` connects the same way to publish a job. The broker host, port, and username are constants in `app/api/routes/print.py`; the password comes from `MQTT_PASSWORD` or Secret Manager. This works for a one-off listing but not for search or autocomplete (a broker round trip per keystroke), every API worker holds its own broker connections, and a deployment without the broker cannot run the print routes at all.

## Proposal

Follow the device-registry pattern used by AWS IoT Device Shadow, Azure IoT Hub device twins, and Home Assistant: keep the last reported state of each device in the database, written by one subscriber, and have the API read only the database.

1. **`printer` table.** `printer_id` (unique), last reported `state`, `last_seen_at`, and room for admin-set fields (display name, location) if the Hub needs them. Optionally, admins can register printers that have never reported.
2. **One status writer.** A subscriber keeps a single MQTT subscription to `crucible-printer/+/status` and upserts the row on every message, so the table is current within seconds. It ships in the API image and runs either:
   - as its own process (`python -m app.workers.printer_status`): a one-replica Deployment on GKE, or another service in docker-compose; or
   - inside the API process for single-container installs, where each worker tries a Postgres advisory lock at startup and only the holder subscribes; if it dies the lock is released and another worker takes over.
3. **API reads the database.** `GET /print/printers` gains `q` (case-insensitive substring on `printer_id`, and the display name if added), an `online` filter, sorting, and pagination like other collections. `online` is computed from `last_seen_at` with the existing 90 s rule. `POST /print/barcode` can reject an unknown `printer_id` before publishing. No request opens a broker connection just to list printers.
4. **Errors.** If the registry is empty because the writer is down, the API should be able to tell (for example a writer heartbeat row) and return 503 rather than an empty list.

## Self-hosting

Label printing is a Molecular Foundry integration and must stay optional, in line with decoupling Crucible from GCS (`docs/multipart_storage_backends.md`):

- A setting such as `PRINTING_ENABLED` (off by default) gates the print routes and the writer. When off, the routes return a clear "not enabled" response and nothing connects to a broker.
- Broker host, port, username, password, and TLS come from the environment, not code constants.
- The core API depends only on Postgres; the printer writer is a pluggable integration, the same principle as the storage adapter.

## Later

If instruments start reporting heartbeats, the table can generalize to device status with the printer subscriber as its first writer. Not needed now.
