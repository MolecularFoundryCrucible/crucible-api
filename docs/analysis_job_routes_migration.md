# Analysis job routes migration

## Status

Superseded (2026-10-16). Implemented as postprocessing pipelines: `GET /pipelines`, `POST /datasets/{dataset_mfid}/postprocess` (contributor access, optional `Idempotency-Key`), and tracked runs under `/postprocessing_runs`. The four routes below are deprecated wrappers that run the same checks and return the run. The rest of this document is kept for history.

## Affected routes

```http
POST /datasets/{dataset_mfid}/carrier_segmentation
POST /datasets/{dataset_mfid}/insitu_spec_aggregation
POST /datasets/{dataset_mfid}/rga_analysis
POST /datasets/{dataset_mfid}/mosaic_stitch
```

These paths are used by Nano, Graph Explorer, and Crucible Upload UIs and should remain available during the compatibility migration.

## Current problems

- Carrier segmentation and in-situ aggregation do not verify that the dataset exists.
- RGA analysis and mosaic stitching verify dataset existence but do not verify resource permission.
- Any authenticated caller can currently enqueue processing for a dataset they cannot modify.
- The routes return an empty `200 OK` even though processing is asynchronous.
- Retried requests can enqueue duplicate work because there is no idempotency or persisted analysis-request record.

## Immediate future hardening

Keep the existing paths and route all four handlers through one internal enqueue helper. Each route should:

1. Return `404 Not Found` when the dataset does not exist.
2. Require `Permission.CREATE`, which means contributor or above on the dataset.
3. Publish the existing RabbitMQ message only after authorization succeeds.
4. Return `202 Accepted` with a typed response.

Proposed response:

```json
{
  "dataset_mfid": "01...",
  "analysis_type": "carrier_segmentation",
  "status": "accepted"
}
```

The response must say `accepted`, not `completed` or `queued`, because the current API only knows that it submitted the message to its RabbitMQ client. It does not track worker state.

This response is compatible with current callers: Nano already exposes a dictionary result, Graph Explorer passes the result through, and Upload UIs do not depend on the empty response body.

## Later task tracking

Do not return a job identifier until the API persists a real analysis request. Proper tracking should include:

- A durable analysis-request table with request ID, dataset MFID, analysis type, requester, state, timestamps, and failure details.
- An idempotency key accepted from clients and enforced with a database uniqueness constraint.
- The request ID in every RabbitMQ message.
- Worker callbacks or events that record started, completed, and failed states.
- A read route for request status.
- A retry policy that distinguishes safe redelivery from an intentional rerun.

Prefect currently retries post-processing requests. Idempotency is therefore important, but implementing only an in-memory or time-based duplicate check would be unreliable across API instances and deployments.
