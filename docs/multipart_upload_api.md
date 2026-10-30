# Signed multipart uploads

Existing resumable and token-based multipart routes retain their schemas. New sessions must use the new route family and cannot be completed through the legacy endpoint. The API performs every storage operation inside the request; there is no background worker.

## HTTP workflow

All routes accept either an existing Crucible API key or a Crucible access token issued by the ORCID/OAuth login flow, using `Authorization: Bearer <credential>` and the shared dataset permission system. ORCID provider tokens and refresh tokens are not API access credentials. A refreshed login token for the same principal can resume an existing upload; revoked or expired login access tokens are rejected. Every response in the multipart namespace has `Cache-Control: no-store`. No response contains an OAuth token or service-account credential.

| Method and path | Request | Success |
| --- | --- | --- |
| `POST /datasets/{dataset_mfid}/upload/multipart/initiate` | `filename`, integer `size` (0 for an empty file), optional lowercase `sha256_hash`; optional `Idempotency-Key` header | `200` with state `uploading`, or state `completed` with `existing_file` for a trusted deduplication hit; `503` with `Retry-After` when storage is unavailable |
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
  "part_size": 16777216,
  "part_count": 8,
  "parts": [],
  "next_part_number_marker": null,
  "existing_file": null,
  "failure_code": null
}
```

Session states are `uploading`, `completed`, `aborted`, `failed`, and `expired`. `initiating` and `finalizing` are transient states held only while an initiate or complete request is in flight; a client that sees `finalizing` after a lost response retries completion.

The object key is server-generated and independent of the logical filename. Objects are stored at `{dataset_mfid}/_files/{upload_id}`; the `_files` segment is reserved, so legacy uploads cannot write there and legacy tokens cannot reach it. Filenames must be relative paths without empty or dot segments, backslashes, or control characters. New sessions reject an existing or reserved filename with 409, and the legacy upload and file-registration routes reject a filename reserved by an active multipart session. An idempotency key is scoped to owner, dataset, and protocol; changing its request fields returns 409. Keep the same key across initiation retries: a retry after a lost response reuses the provider upload instead of creating another. Sessions expire after seven days; an initiation that never completes its storage call releases its reservation after ten minutes.

The API chooses the part size per session and returns it as `part_size`: the file size divided by 8, rounded up to a whole MiB, between 8 MiB and 64 MiB, so files up to 512 MiB split into about 8 parts. Every part is exactly `part_size` bytes except the final one. Plan parts from the returned `part_size`, not a client-side constant. Maximum file size is 500 GiB. An empty file (`size` 0) has `part_size` 0 and `part_count` 0: request no part URLs and complete directly with the SHA-256 and CRC32C of empty content (`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` and `AAAAAA==`); the API checks both and writes the empty object itself. Compute the canonical base64 CRC32C of each part before requesting its URL. Once authorized, that checksum cannot change. A URL signs PUT, the exact destination, upload ID, part number, checksum header, and expected content length. URLs expire after at most 15 minutes and never after session expiry; refresh expired URLs with the same checksums. The batch endpoint enforces a configurable per-session interval (five seconds by default), plus an owner session quota that bounds aggregate issuance. Request about 32 URLs per call and upload about 8 parts concurrently so the signing interval does not cap throughput.

Send every header returned for a part except `Content-Length`; do not hard-code provider header names. In browsers pass the exact Blob slice as the body and let the browser set `Content-Length`. Bucket CORS must allow the chosen frontend origins, PUT, and the returned request headers. Signed URLs are bearer capabilities: keep them out of logs, analytics, persistent resume files, and error reporting. The provider part-upload contract is documented by [Google](https://docs.cloud.google.com/storage/docs/xml-api/put-object-multipart).

Resume by following every `next_part_number_marker`, skipping acknowledged part numbers, and requesting URLs for missing parts. A provider ETag is an opaque completion identifier, not a SHA-256 digest. Completion lists all provider pages, compares part numbers and sizes with the session geometry and authorized checksums, assembles the object, and verifies its size and CRC32C before publishing the associated file and aborting any leftover provider uploads for that object. Large uploads can take several seconds to complete; use a request timeout of about two minutes. Completion is safe to retry: an already assembled object is verified and published, and an already published file is returned. Missing or mismatched parts and checksums fail the session, abort its provider uploads, and delete any unpublished object; start a fresh session after correcting the source.

The completion SHA-256 claim is recorded unverified (`sha256_verified_at` stays null) and is excluded from trusted deduplication on every upload route. A separate verification job may fill verification timestamps later. CRC32C is enforced per part by the provider and for the whole object by the API.

Current ownership and CREATE permission are checked for status, signing, and completion, using the request's privilege mode. Owners may abort after dataset access is revoked. Already issued URLs remain usable until expiry or provider abort. Completed sessions retain their file identifier for retries; if that file has been deleted, completion returns 410. Deleting a file or hard-deleting a dataset removes database records only, as on the legacy routes; storage objects are not deleted. Editing file identity metadata clears trust metadata; changing its location clears its registered generation. Downloads and ingestion serve only objects under the file's own dataset in an allowed bucket, and multipart objects additionally require that internal generation; see [storage location confinement](storage_download_design.md).

## Replacing a file

`POST /files/{file_mfid}/upload/multipart/initiate` replaces an existing GCS file's content through this same multipart flow: the caller needs EDIT on the file's dataset, may send `If-Match` with the file's current `etag` (or `*` for any) to refuse a stale replace, and parts, status, abort, and completion use the dataset's existing routes. A stale `If-Match` returns 412 `etag_mismatch`, a concurrent replace on the same file 409 `replace_in_progress`, a change to the file (including its generation, hash, size, or backend) between initiation and completion 409 `file_changed`, lost EDIT before completion 403 `permission_revoked`, and a catalog-only (non-GCS) file 400. Completion keeps the file's MFID and filename and returns the same `AssociatedFileRead` with the new content and `etag`. The previous object is retired rather than deleted immediately; see [file replacement](file_replace_design.md) for the full flow, ETag definition, and retirement rules.

## Reference client

`python -m scripts.multipart_upload_client --api https://host/api-root --dataset DATASET_MFID --file data.h5 --state upload-resume.json`

Set `CRUCIBLE_API_TOKEN` in the environment. The resume record contains a source fingerprint, idempotency key, and application upload ID, with no credentials or signed URLs. Use one client process per resume record. The client scans the source for SHA-256, whole-object CRC32C, and per-part CRC32C, signs 32 parts per call, uploads 8 parts in parallel, and completes once. Rerunning resumes the same session and skips acknowledged parts. Keep the source file immutable. Nano itself is maintained outside this repository and has not been released with this transport.

## Storage configuration

| Variable | Default / purpose |
| --- | --- |
| `STORAGE_BACKEND` | `gcs` (Google Cloud Storage); `s3` for an S3-compatible store |
| `UPLOAD_BUCKET` | `mf-storage-prod`; the environment's single upload bucket, shared by legacy and multipart uploads (staging: `mf-storage-staging`) |
| `STORAGE_READ_BUCKETS` | Optional comma-separated extra buckets that existing records may point at (staging: `mf-storage-prod`) |
| `MULTIPART_UPLOAD_ENABLED` | `false`; controls new initiation only |
| `MULTIPART_UPLOAD_BUCKET` | Required when enabled; must equal `UPLOAD_BUCKET` |
| `GCS_SA` | Optional on `gcs`: a service-account key (secret); without it the server uses Application Default Credentials |
| `MULTIPART_UPLOAD_SIGNER` | Optional on `gcs`: service account that signs URLs by impersonation; needed when the server's identity is not a service account (GKE Workload Identity, Workload Identity Federation) |
| `MULTIPART_UPLOAD_PROJECT` | Optional on `gcs`: project for the storage client; defaults to `GCS_PROJECT` |
| `S3_ENDPOINT_URL` | Required on `s3`: the address the API reaches the store on |
| `S3_PUBLIC_URL` | On `s3`: the address in signed URLs, which clients must reach; defaults to `S3_ENDPOINT_URL` |
| `S3_REGION` | On `s3`: `us-east-1` |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | Required on `s3`, as secrets |
| `MAX_UPLOAD_BYTES` | Existing application size limit, capped at 500 GiB for this protocol |
| `MULTIPART_OWNER_SESSIONS` | 8 concurrent reservations |
| `MULTIPART_DATASET_SESSIONS` | 32 concurrent reservations |
| `MULTIPART_OWNER_BYTES` | 1 TiB reserved bytes |
| `MULTIPART_DATASET_BYTES` | 4 TiB reserved bytes |
| `MULTIPART_SIGNING_INTERVAL_SECONDS` | 5 seconds per session |

On `gcs`, part URLs are signed as `MULTIPART_UPLOAD_SIGNER` by impersonation when it is set (the runtime identity needs Service Account Token Creator on it), otherwise as the server's own service account: locally with a key, or through IAM signBlob (Token Creator on itself). The signing account needs object and multipart permissions on the upload bucket. Multipart objects share the bucket with legacy objects under the reserved `{dataset_mfid}/_files/` segment; the downscoped legacy upload token excludes that segment, so legacy token holders cannot read or overwrite them. [Google impersonation reference](https://google-auth.readthedocs.io/en/latest/reference/google.auth.impersonated_credentials.html)

On `s3`, part URLs are presigned with SigV4 and bind the part's CRC32C and length (`x-amz-checksum-crc32c` and `Content-Length`, both returned as headers to send), so the store refuses a part with other content. Many S3 servers do not report a whole-object CRC32C, so the API computes it from the authorized part checksums and sizes (with `awscrt`'s `combine_crc32c`) and compares it with the completion claim. When the store lists each part's own CRC32C (Versity S3 Gateway does; SeaweedFS 4.51 does not), the API first compares those with the authorized values, so the check does not depend on the store enforcing the signed checksum; otherwise it does. A store is supported only if `python -m app.cli storage check` passes on it, which proves that it refuses a part with the wrong checksum. Objects are recorded with `storage_backend` `"gcs"`, the historical name for "stored by the API", which released clients require. Signed URLs embed `S3_PUBLIC_URL`: a client that reaches the server by another address gets signature errors. On `s3`, a replace records the previous content in `file_versions` in the same transaction that repoints the file; `python -m app.cli storage cleanup` (run hourly; it does nothing unless `STORAGE_CLEANUP_ENABLED=true`, off by default) deletes versions older than `FILE_VERSION_RETENTION_DAYS` (default 14) that no file uses, and aborts expired or uncleaned multipart uploads, and `python -m app.cli files restore FILE_MFID` makes the newest kept version current again. `python -m app.cli storage check` uploads, downloads, and deletes a test file under `_crucible-check/` with the configured store (`--public` signs for `S3_PUBLIC_URL`); CI runs it against SeaweedFS and Versity S3 Gateway.

The Google environment bucket also needs a lifecycle rule `{"action": {"type": "Delete"}, "condition": {"daysSinceCustomTime": 7}}` that removes objects retired by a file replacement.

The bucket lifecycle rule that aborts incomplete multipart uploads is required: it is the only cleanup for sessions that are abandoned or expire without an abort request. Set its age beyond the seven-day session lifetime. Failed verification and abort requests clean up immediately; when storage is unavailable during that cleanup, the lifecycle rule remains the fallback. Session records are retained; the scheduling columns `next_attempt_at`, `lease_owner`, `lease_expires_at`, `cleanup_requested_at`, and `attempts` are unused. Inspect `state`, `failure_code`, and `cleaned_at` for operations. GCS holds and retention can prevent object deletion and require operator intervention.

The ingestion message retains `filename` as the full bucket/object path, adds `storage_generation` for new files, and carries the record's display name as `file_name`. External ingestion workers must resolve the environment's upload bucket (and any `STORAGE_READ_BUCKETS`), avoid relying on the object-key suffix as a file type, and may read the live object because multipart objects are never overwritten. Consumers must take names and types from `file_name`, never from the object key, which is the opaque upload identifier. Explicit `ingestion_class` may be needed for consumers that previously inferred it from the filename. Consumers need a mount for each environment bucket and must take names from `file_name` before multipart files can be ingested.

## Deployment

1. The schema head is `c83a7d014b29`. Staging is already migrated to it. Apply both multipart revisions on production through the authorized release workflow before deploying, and deploy the legacy completion guard everywhere before enabling initiation.
2. Provision the environment-specific bucket (uniform access, public access prevention, CORS, lifecycle abort of incomplete uploads), the signer, and the runtime impersonation grant. Staging uses `mf-crucible-multipart-staging` and `crucible-mpu-signer-staging@mf-crucible.iam.gserviceaccount.com`.
3. Resolve legacy-identity isolation before production: the legacy token identity is currently a project Owner and therefore reaches any bucket in the project.
4. Exercise the reference client, target browsers, download and ingestion consumers, network interruption, abort and completion races, and representative large files.
5. Enable a small client cohort, monitor failures, and migrate Nano and other clients separately. No legacy route is retired by this implementation.

The local tests use a deterministic storage adapter for lifecycle tests, mocked XML responses for adapter contract checks, and disposable PostgreSQL for migrations and concurrency. Signing, per-part checksum rejection, assembly, CRC32C verification, generation-pinned download, and deletion were smoke-tested from the staging API pod against the staging bucket.

The storage calls go through the `ObjectStore` interface in `app/storage/` (`GoogleStore` and `S3Store`). Server-side SHA-256 verification is not implemented; `sha256_hash` is the client's claim. See [S3-compatible storage backends](multipart_storage_backends.md) for portability notes.
