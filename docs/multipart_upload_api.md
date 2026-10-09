# Signed multipart uploads

Implemented in the multipart feature branch. Deployment and the client rollout remain separate release steps. Existing resumable and token-based multipart routes retain their schemas. New sessions must use the new route family and cannot be completed through the legacy endpoint.

## HTTP workflow

All routes accept either an existing Crucible API key or a Crucible access token issued by the ORCID/OAuth login flow, using `Authorization: Bearer <credential>` and the shared dataset permission system. ORCID provider tokens and refresh tokens are not API access credentials. A refreshed login token for the same principal can resume an existing upload; revoked or expired login access tokens are rejected. Every response in the multipart namespace has `Cache-Control: no-store`. No response contains an OAuth token or service-account credential.

| Method and path | Request | Success |
| --- | --- | --- |
| `POST /datasets/{dataset_mfid}/upload/multipart/initiate` | `filename`, positive integer `size`, optional lowercase `sha256_hash`; optional `Idempotency-Key` header | `202` while initializing, `200` for a retry of an initialized session or a trusted deduplication hit |
| `POST /datasets/{dataset_mfid}/upload/multipart/{upload_id}/parts` | `parts`: 1-64 objects containing `part_number` and `crc32c` | `200` signed PUT URLs, required headers, offsets, sizes, and expirations |
| `GET /datasets/{dataset_mfid}/upload/multipart/{upload_id}` | Optional `part_number_marker` and `limit` (1-1000) | `200` session state, provider-acknowledged parts while uploading, pagination marker, completed file or failure code; `409` while a provider-abort reconciliation is pending; `503` while upload progress is temporarily unavailable |
| `POST /datasets/{dataset_mfid}/upload/multipart/complete` | `upload_id`, lowercase `sha256_hash`, canonical base64 `crc32c` | `202` while processing; `200 AssociatedFileRead` after CRC32C verification |
| `DELETE /datasets/{dataset_mfid}/upload/multipart/{upload_id}` | No body | `202` while cleanup is pending; `204` after cleanup; `409` if already published |

Initiation is asynchronous: a database record reserves the filename and quota before the worker talks to GCS. Poll the returned `Location` until state is `uploading`. This also lets the worker recover an initiation whose provider response was lost. The 202 responses provide `Retry-After: 5`. Disabling new initiation leaves existing sessions usable for status, signing, completion, and abort.

```json
{
  "upload_id": "server-generated-opaque-id",
  "upload_protocol": "xml-multipart-signed-v1",
  "state": "initiating",
  "expires_at": "2026-10-05T12:00:00Z",
  "part_size": 67108864,
  "part_count": 2,
  "parts": [],
  "next_part_number_marker": null,
  "existing_file": null,
  "failure_code": null
}
```

The object key is server-generated and independent of the logical filename. Filenames must be relative paths without empty or dot segments, backslashes, or control characters. New sessions reject an existing or reserved filename with 409. Existing legacy duplicates are not modified. An idempotency key is scoped to owner, dataset, and protocol; changing its request fields returns 409. Keep the same key across initiation retries.

Parts are fixed at 64 MiB, except the final part. Maximum file size is 500 GiB. Compute the canonical base64 CRC32C of each part before requesting its URL. Once authorized, that checksum cannot change. A URL signs PUT, the exact destination, upload ID, part number, checksum header, and expected content length. URLs expire after at most 15 minutes and never after session expiry. Upload at most four parts concurrently initially; refresh expired URLs with the same checksums. The batch endpoint enforces a configurable per-session interval (five seconds by default), plus an owner session quota that bounds aggregate issuance. Observe 429 `Retry-After` responses.

For browsers, pass the returned `x-goog-hash` header and the exact Blob slice as the body. Do not manually set `Content-Length`; the browser supplies it from the Blob. Bucket CORS must allow the chosen frontend origins and PUT, and expose ETag and checksum headers. Signed URLs are bearer capabilities: keep them out of logs, analytics, persistent resume files, and error reporting. The provider part-upload contract is documented by [Google](https://docs.cloud.google.com/storage/docs/xml-api/put-object-multipart).

Resume by following every `next_part_number_marker`, skipping acknowledged part numbers, and requesting URLs for missing parts. A provider ETag is an opaque completion identifier, not a SHA-256 digest. Completion lists all provider pages and freezes the ordered manifest, compares part sizes, completes the object, and verifies size and CRC32C before publishing the associated file. The completion SHA-256 claim is recorded unverified and excluded from trusted deduplication; a later background worker may fill verification timestamps over time. Missing/mismatched parts or checksums produce a failed session with cleanup intent; start a fresh session after correcting the source. No automatic fallback to the legacy token route is implemented.

Current ownership and CREATE permission are checked for status, signing, completion, and publication. Owners may abort after dataset access is revoked. Already issued URLs remain usable until expiry or provider abort. Completed sessions retain their file identifier for retries. If that file has subsequently been deleted, completion returns 410. Dataset deletion and file deletion preserve durable cleanup intent. Editing file identity metadata clears trust metadata; changing its location clears its registered generation. Dedicated-bucket download and ingestion requests require that internal generation.

## Reference client

`python -m scripts.multipart_upload_client --api https://host/api-root --dataset DATASET_MFID --file data.h5 --state upload-resume.json`

Set `CRUCIBLE_API_TOKEN` in the environment. The resume record contains a source fingerprint, idempotency key, and application upload ID, with no credentials or signed URLs. Use one client process per resume record. The client scans the source for SHA-256, whole-object CRC32C, and per-part CRC32C, uploads in batches with four parallel transfers, and polls completion. Rerunning resumes the same session and skips acknowledged parts. Keep the source file immutable. This is an integration example; Nano itself is maintained outside this repository and has not been released with this transport.

## Worker and storage configuration

Run `python -m scripts.multipart_worker` as a separately supervised process from the same application image and revision. `--once` processes at most one due session. It uses the API's database configuration and must keep running while initiation is disabled to drain and clean existing sessions. A ten-minute renewable database lease fences stale workers. Provider calls occur outside database transactions, and part-listing pages renew the lease between bounded reads. PostgreSQL row locks serialize idempotency, filename reservations, and quota admission; a transaction advisory lock bounds concurrent worker claims. Expired leases permit another worker to recover after a crash.

| Variable | Default / purpose |
| --- | --- |
| `MULTIPART_UPLOAD_ENABLED` | `false`; controls new initiation only |
| `MULTIPART_UPLOAD_BUCKET` | Required dedicated destination, never the legacy production bucket |
| `MULTIPART_UPLOAD_SIGNER` | Required dedicated service-account email |
| `MULTIPART_UPLOAD_PROJECT` | Required explicit project |
| `MAX_UPLOAD_BYTES` | Existing application size limit, capped at 500 GiB for this protocol |
| `MULTIPART_OWNER_SESSIONS` | 8 concurrent reservations |
| `MULTIPART_DATASET_SESSIONS` | 32 concurrent reservations |
| `MULTIPART_OWNER_BYTES` | 1 TiB reserved bytes |
| `MULTIPART_DATASET_BYTES` | 4 TiB reserved bytes |
| `MULTIPART_WORKER_CONCURRENCY` | 2 active worker leases globally across replicas |
| `MULTIPART_SIGNING_INTERVAL_SECONDS` | 5 seconds per session |

Application Default Credentials impersonate the configured dedicated signer using Google's authentication library. The legacy `GCS_SA` secret is never used by this adapter. The API and worker runtime identities need the appropriate impersonation grant, and the target signer needs object and multipart permissions on the dedicated bucket. Never grant the legacy token identity access to this bucket, including through inherited IAM. [Google impersonation reference](https://google-auth.readthedocs.io/en/latest/reference/google.auth.impersonated_credentials.html)

Workers emit application upload IDs, states, and bounded failure codes, without provider responses or capabilities. Inspect `state`, `failure_code`, `attempts`, `next_attempt_at`, `lease_expires_at`, `cleanup_requested_at`, and `cleaned_at` for operational backlog. Transient failures retry with bounded exponential delay; expiry schedules cleanup. Reservations remain charged until terminal cleanup succeeds. Terminal cleanup is revisited hourly to catch late provider completion and orphaned uploads. Completed sessions also receive an hourly sweep that aborts leftover multipart uploads without deleting their published object. Records are retained in this first release; automatic pruning is deliberately not enabled. Configure bucket lifecycle abort of incomplete uploads as an independent fallback. GCS holds and retention can prevent object deletion and require operator intervention.

The ingestion message retains `filename` as the full bucket/object path and adds `storage_generation` for new files. External ingestion workers must accept the dedicated bucket, avoid relying on the object-key suffix as a file type, and honor the generation when reading. Their deployment, credentials, and GCS mounts are outside this repository and remain pilot acceptance checks. Explicit `ingestion_class` may be needed for consumers that previously inferred it from the filename.

## Deployment gates

1. Auth integration is complete through `origin/main` at `8e4d0ef`. The migration chain is `e92a6b1c730f` (auth) -> `b7e2d9c4a1f3` (staging slug indexes) -> `b72f6c903a18` -> `c83a7d014b29` (multipart head). Review both unapplied multipart revisions before release.
2. Apply the additive migrations through the separately authorized release workflow before deploying the application and its updated `EXPECTED_DB_REVISIONS=c83a7d014b29` configuration. Coordinate readiness pins during the migration window, since existing deployments check exact schema revisions. Deploy the legacy completion guard to every instance before enabling initiation.
3. Provision the permanent environment-specific bucket, private access controls, CORS, lifecycle policy, signer, and runtime impersonation grants. Test legacy-identity denial and dedicated-identity access in that environment.
4. Deploy and observe the worker with initiation disabled. Configure edge request/body limits and a worker replica count consistent with the database concurrency budget. Keep clocks synchronized for signed URLs and leases. A later background worker that fills SHA-256 verification timestamps would need its own replica budget.
5. Exercise the reference client, target browsers, real download and ingestion consumers, network interruption, pod restart, abort/finalize races, cleanup and retention failures, and representative large files. Upload throughput is no longer bounded by a verification read; measure cohort rollout on transfer behavior alone.
6. Enable a small client cohort, monitor adoption and failures, and migrate Nano/other clients separately. No legacy route is retired by this implementation.

The local tests use a deterministic storage adapter for application lifecycle tests, mocked XML responses for adapter contract checks, and disposable PostgreSQL for migrations and concurrency. Stage 1 separately exercised real GCS signing, browser behavior, and experimental bucket isolation. The new application adapter has not yet passed a live deployed staging pilot; no production-readiness claim follows from these local checks.

## Validation record

The full local suite passed with 1,253 tests and 63 environment-dependent skips at the initial implementation. The focused multipart suite has since grown to 36 tests plus PostgreSQL concurrency and two-revision migration round trips; at HEAD it runs 1,546 tests with 63 environment-dependent skips. The reference-client test simulates a lost part response, reruns from its local resume record, skips the accepted part, and receives the CRC32C-verified file. Recovery tests cover lost initiation/completion responses, a failed publication transaction, revoked access, lease takeover, abort before processing, expiry, and dataset/file deletion. Provider adapter tests check signature arguments, XML errors inside HTTP 200, pagination, and generation pinning.

The generated OpenAPI uses the proxy root path of the generating configuration and adds five operations across four paths; every existing path and component schema is unchanged. Pyflakes on the new modules and `git diff --check` passed. Alembic reports the single branch head `c83a7d014b29`. Validation used dummy application credentials and an isolated local PostgreSQL container, with dotenv loading disabled; no shared database or cloud storage was written by this implementation phase.

Two deliberate leftovers from the integrity-policy change: the `verifying` session state remains admitted by the schema and routed to finalization, so a session stranded by an older deployment publishes instead of hot-looping, and `MultipartStorage.sha256` is retained as the generation-pinned primitive the future verification worker will use when filling the verification timestamps.
