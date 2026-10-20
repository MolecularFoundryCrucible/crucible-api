# Message queue publishing (ingestion and postprocessing)

Status: documented 2026-10-20, not scheduled. Ingestion and postprocessing will be reworked together later; this note records how publishing behaves today, what can go wrong, and what the rework has to cover.

## How it works today

These routes publish a RabbitMQ message from inside the API request:

| Route | Publisher | Routing key |
| --- | --- | --- |
| `POST /files/{file_id}/ingest` (and deprecated `POST /datasets/{dataset_mfid}/files/{file_id}/ingest`) | `create_file_ingestion_request` in `app/api/routes/files.py` | `ingestion-{RMQ_ROUTING_SUFFIX}-{small,medium,large}`, tier from file size |
| `POST /datasets/{dataset_mfid}/postprocess`, and the four deprecated analysis routes (`carrier_segmentation`, `insitu_spec_aggregation`, `rga_analysis`, `mosaic_stitch`) | `start_postprocessing_run` and `_publish` in `app/api/routes/pipelines.py` | the pipeline's routing key |

Each publish goes through `send_pika_message` in `app/utils/rmq_utils.py`, which opens a new `pika.BlockingConnection`, publishes once to the default exchange, and closes the connection. The route waits for this before responding.

## Known problems

1. **No timeouts.** `pika.ConnectionParameters` is built with only host, port, virtual host, and credentials, so `socket_timeout`, `stack_timeout`, and `blocked_connection_timeout` are pika's defaults. An unreachable broker, a lossy network, or a broker in a memory or disk alarm (which blocks publishers; `blocked_connection_timeout` defaults to no limit) can hold the request, and its worker thread, for a long time or indefinitely. Enough of these exhaust the worker pool and stall unrelated requests.
2. **Connection leak on error.** `connection.close()` runs only after a successful publish; an exception leaves the connection open until garbage collection.
3. **Secret lookup per request.** Both publishers call `get_secret("RABBITMQ_DEFAULT_PW", ...)` on every request. `get_secret` (`app/general_utils.py`) calls `load_dotenv()` each time, re-reading `.env` from disk. The value is already resolved at import as `rmq_utils.rmq_pw`, but `send_pika_message` defaults its `rmq_pw` parameter to `''`, which shadows it, so callers that omit it would connect with an empty password.
4. **Ingestion leaves a stale request when publishing fails.** The `IngestionRequest` row is committed with status `requested` before the publish. If publishing raises, the caller gets a 500 and the row stays `requested` with nothing queued; nothing retries it or marks it failed. Postprocessing already handles this: `_queue` marks the run `failed` with `error="Could not queue the run"` and returns 503 `pipeline_unavailable`, and a retry with the same `Idempotency-Key` re-queues it.
5. **Commit and publish are not atomic.** Even with failure handling, a crash between the database commit and the publish (or a publish that succeeds while the response fails) leaves the database and the queue disagreeing. Both paths are exposed to this.
6. **Ingestion status vocabulary is undefined.** `IngestionRequest.status` is free text. The API writes `requested` and `not_requested`; consumers update it through `PATCH /ingestion_requests/{request_id}` with whatever they choose. A failure status for the API to set would have to match what consumers and clients expect.

## What the rework should cover

- Explicit connection timeouts (`socket_timeout`, `stack_timeout`, `blocked_connection_timeout`, a few seconds each, configurable), and `with pika.BlockingConnection(...)` so the connection always closes.
- Resolve the broker password once and stop calling `get_secret` per request; default `send_pika_message` to the module-level credentials.
- The same failure contract for ingestion as for postprocessing: mark the request failed, return 503 with a stable code, and support idempotent retries.
- A defined status set for ingestion requests (and the same for postprocessing runs, which already use `requested`, `running`, `completed`, `failed`), shared with the consumers.
- Decide whether to keep publishing inside the request or move to a transactional outbox: write the request row and an outbox row in one transaction, and let a background publisher deliver and mark them sent. That removes the commit/publish gap and keeps broker outages out of request latency.
- Whether ingestion and postprocessing should share one run model and one publishing path, given they stay separate features (ingestion extracts metadata from large files; postprocessing creates new resources or does heavy computation).
