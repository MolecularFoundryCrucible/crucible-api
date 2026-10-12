# Signed multipart uploads

Existing resumable and token-based multipart routes retain their schemas. New sessions must use the new route family and cannot be completed through the legacy endpoint. The API performs every storage operation inside the request; there is no background worker.

## HTTP workflow

All routes accept either an existing Crucible API key or a Crucible access token issued by the ORCID/OAuth login flow, using `Authorization: Bearer <credential>` and the shared dataset permission system. ORCID provider tokens and refresh tokens are not API access credentials. A refreshed login token for the same principal can resume an existing upload; revoked or expired login access tokens are rejected. Every response in the multipart namespace has `Cache-Control: no-store`. No response contains an OAuth token or service-account credential.

| Method and path | Request | Success |
| --- | --- | --- |
| `POST /datasets/{dataset_mfid}/upload/multipart/initiate` | `filename`, positive integer `size`, optional lowercase `sha256_hash`; optional `Idempotency-Key` header | `200` with state `uploading`, or state `completed` with `existing_file` for a trusted deduplication hit; `503` with `Retry-After` when storage is unavailable |
| `POST /datasets/{dataset_mfid}/upload/multipart/{upload_id}/parts` | `parts`: 1-64 objects containing `part_number` and `crc32c` | `200` signed PUT URLs, required headers, offsets, sizes, and expirations; `429` with `Retry-After` inside the signing interval |
| `GET /datasets/{dataset_mfid}/upload/multipart/{upload_id}` | Optional `part_number_marker` and `limit` (1-1000) | `200` session state, provider-acknowledged parts while uploading, pagination marker, completed file or failure code; `409` when the provider upload no longer exists; `503` while progress is temporarily unavailable |
| `POST /datasets/{dataset_mfid}/upload/multipart/complete` | `upload_id`, lowercase `sha256_hash`, canonical base64 `crc32c` | `200 AssociatedFileRead` after size and CRC32C verification; `422` when verification fails; `409` when aborted or not publishable; `503` with `Retry-After` when storage is unavailable |
| `DELETE /datasets/{dataset_mfid}/upload/multipart/{upload_id}` | No body | `204` after cleanup, repeatable; `409` if already published; `503` with `Retry-After` when storage cleanup must be retried |

```json
{
  "upload_id": "server-generated-opaque-id",
  "upload_protocol": "xml-multipart-signed-v1",
  "state": "uploading",
  "expires_at": "2026-10-19T12:00:00Z",
  "part_size": 67108864,
  "part_count": 2,
  "parts": [],
  "next_part_number_marker": null,
  "existing_file": null,
  "failure_code": null
}
```

Session states are `uploading`, `completed`, `aborted`, `failed`, and `expired`. `initiating` and `finalizing` are transient states held only while an initiate or complete request is in flight; a client that sees `finalizing` after a lost response retries completion.

The object key is server-generated and independent of the logical filename. Filenames must be relative paths without empty or dot segments, backslashes, or control characters. New sessions reject an existing or reserved filename with 409, and the legacy upload and file-registration routes reject a filename reserved by an active multipart session. An idempotency key is scoped to owner, dataset, and protocol; changing its request fields returns 409. Keep the same key across initiation retries: a retry after a lost response reuses the provider upload instead of creating another. Sessions expire after seven days; an initiation that never completes its storage call releases its reservation after ten minutes.

Parts are fixed at 64 MiB, except the final part. Maximum file size is 500 GiB. Compute the canonical base64 CRC32C of each part before requesting its URL. Once authorized, that checksum cannot change. A URL signs PUT, the exact destination, upload ID, part number, checksum header, and expected content length. URLs expire after at most 15 minutes and never after session expiry; refresh expired URLs with the same checksums. The batch endpoint enforces a configurable per-session interval (five seconds by default), plus an owner session quota that bounds aggregate issuance. Request about 32 URLs per call and upload about 8 parts concurrently so the signing interval does not cap throughput.

Send every header returned for a part except `Content-Length`; do not hard-code provider header names. In browsers pass the exact Blob slice as the body and let the browser set `Content-Length`. Bucket CORS must allow the chosen frontend origins, PUT, and the returned request headers. Signed URLs are bearer capabilities: keep them out of logs, analytics, persistent resume files, and error reporting. The provider part-upload contract is documented by [Google](https://docs.cloud.google.com/storage/docs/xml-api/put-object-multipart).

Resume by following every `next_part_number_marker`, skipping acknowledged part numbers, and requesting URLs for missing parts. A provider ETag is an opaque completion identifier, not a SHA-256 digest. Completion lists all provider pages, compares part numbers and sizes with the session geometry and authorized checksums, assembles the object, and verifies its size and CRC32C before publishing the associated file and aborting any leftover provider uploads for that object. Large uploads can take several seconds to complete; use a request timeout of about two minutes. Completion is safe to retry: an already assembled object is verified and published, and an already published file is returned. Missing or mismatched parts and checksums fail the session, abort its provider uploads, and delete any unpublished object; start a fresh session after correcting the source.

The completion SHA-256 claim is recorded unverified (`sha256_verified_at` stays null) and is excluded from trusted deduplication on every upload route. A separate verification job may fill verification timestamps later. CRC32C is enforced per part by the provider and for the whole object by the API.

Current ownership and CREATE permission are checked for status, signing, and completion, using the request's privilege mode. Owners may abort after dataset access is revoked. Already issued URLs remain usable until expiry or provider abort. Completed sessions retain their file identifier for retries; if that file has been deleted, completion returns 410. Deleting a file or hard-deleting a dataset removes database records only, as on the legacy routes; storage objects are not deleted. Editing file identity metadata clears trust metadata; changing its location clears its registered generation. Dedicated-bucket download and ingestion requests require that internal generation.

## Reference client

`python -m scripts.multipart_upload_client --api https://host/api-root --dataset DATASET_MFID --file data.h5 --state upload-resume.json`

Set `CRUCIBLE_API_TOKEN` in the environment. The resume record contains a source fingerprint, idempotency key, and application upload ID, with no credentials or signed URLs. Use one client process per resume record. The client scans the source for SHA-256, whole-object CRC32C, and per-part CRC32C, signs 32 parts per call, uploads 8 parts in parallel, and completes once. Rerunning resumes the same session and skips acknowledged parts. Keep the source file immutable. Nano itself is maintained outside this repository and has not been released with this transport.

## Storage configuration

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
| `MULTIPART_SIGNING_INTERVAL_SECONDS` | 5 seconds per session |

Application Default Credentials impersonate the configured dedicated signer using Google's authentication library. The legacy `GCS_SA` secret is never used by this adapter. The API runtime identity needs the token-creator grant on the signer, and the signer needs object and multipart permissions on the dedicated bucket. Never grant the legacy token identity access to this bucket, including through inherited IAM. [Google impersonation reference](https://google-auth.readthedocs.io/en/latest/reference/google.auth.impersonated_credentials.html)

The bucket lifecycle rule that aborts incomplete multipart uploads is required: it is the only cleanup for sessions that are abandoned or expire without an abort request. Set its age beyond the seven-day session lifetime. Failed verification and abort requests clean up immediately; when storage is unavailable during that cleanup, the lifecycle rule remains the fallback. Session records are retained; the scheduling columns `next_attempt_at`, `lease_owner`, `lease_expires_at`, `cleanup_requested_at`, and `attempts` are unused. Inspect `state`, `failure_code`, and `cleaned_at` for operations. GCS holds and retention can prevent object deletion and require operator intervention.

The ingestion message retains `filename` as the full bucket/object path and adds `storage_generation` for new files. External ingestion workers must accept the dedicated bucket, avoid relying on the object-key suffix as a file type, and honor the generation when reading. Explicit `ingestion_class` may be needed for consumers that previously inferred it from the filename.

## Deployment

1. The schema head is `c83a7d014b29`. Staging is already migrated to it. Apply both multipart revisions on production through the authorized release workflow before deploying, and deploy the legacy completion guard everywhere before enabling initiation.
2. Provision the environment-specific bucket (uniform access, public access prevention, CORS, lifecycle abort of incomplete uploads), the signer, and the runtime impersonation grant. Staging uses `mf-crucible-multipart-staging` and `crucible-mpu-signer-staging@mf-crucible.iam.gserviceaccount.com`.
3. Resolve legacy-identity isolation before production: the legacy token identity is currently a project Owner and therefore reaches any bucket in the project.
4. Exercise the reference client, target browsers, download and ingestion consumers, network interruption, abort and completion races, and representative large files.
5. Enable a small client cohort, monitor failures, and migrate Nano and other clients separately. No legacy route is retired by this implementation.

The local tests use a deterministic storage adapter for lifecycle tests, mocked XML responses for adapter contract checks, and disposable PostgreSQL for migrations and concurrency. Signing, per-part checksum rejection, assembly, CRC32C verification, generation-pinned download, and deletion were smoke-tested from the staging API pod against the staging bucket.

`MultipartStorage.sha256` is retained as the generation-pinned primitive for a future verification job. See [S3-compatible storage backends](multipart_storage_backends.md) for portability notes.
