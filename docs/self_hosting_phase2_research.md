# Self-hosting Phase 2: storage research (2026-10-28)

Research before planning Phase 2 (one storage layer; an object store in the Compose bundle). Two parts: a map of today's storage code, and an outside survey. Nothing was run against live GCS or an object store; claims about servers come from the cited sources.

## Today's code

- Only the signed multipart upload goes through an adapter (`app/services/multipart_storage.py`, GCS XML API, keyless signing by impersonating `MULTIPART_UPLOAD_SIGNER`). Everything else calls the GCS client directly with the `GCS_SA` key: signed downloads (`app/services/storage_downloads.py`), retiring replaced objects (`customTime` plus a bucket lifecycle rule), record files (`app/utils/gcs_record_utils.py`), the legacy resumable upload (GCS JSON API) and the legacy downscoped upload token (`app/api/routes/gcs_storage.py`).
- GCS-specific behavior relied on: object generations (download pinning, `if_generation_match` on retire and delete, part of the file ETag), CRC32C enforced per part through the signed `x-goog-hash` header, `customTime` retirement with `daysSinceCustomTime: 7`, prefix listing to find legacy objects, the `bucket/key` `storage_path` format, and `storage_backend == "gcs"` switching on signing, ingestion and replace.
- Gaps found: `DELETE /files/{file_id}` and dataset hard delete remove database rows only and leave the objects (`_delete_gcs_blobs_for_dataset` in `app/utils/deletion_utils.py` has no caller); SHA-256 is client-claimed and never verified; thumbnails are in the database, not object storage. Stale docs: `docs/file_object_cleanup_followup.md` and `docs/codebase_review_2026-10.md` say hard delete removes objects; `docs/multipart_upload_api.md` names an old staging bucket and a default `UPLOAD_BUCKET`.
- Tests use fakes only; nothing in CI touches a real object store.

## GCS through its S3-compatible API

- Works with HMAC keys and SigV4: presigned GET, PUT and UploadPart, create, complete, abort and list multipart, HEAD, DELETE, copy ([interoperability](https://docs.cloud.google.com/storage/docs/interoperability), [multipart](https://docs.cloud.google.com/storage/docs/multipart-uploads)).
- Does not: S3 `If-Match` and `If-None-Match` on writes are silently ignored ([ClickHouse #115266](https://github.com/ClickHouse/ClickHouse/issues/115266), [stowage-js #436](https://github.com/stowage-js/stowage/issues/436)); no preconditions on multipart; versions surface as `x-goog-generation`, not `versionId`; bucket CORS cannot be set through the S3 API; no tagging, no UploadPartCopy.
- Unverified: whether a presigned GET can pin a generation, whether `x-goog-if-generation-match` is honored under HMAC, whether CompleteMultipartUpload returns the generation.
- HMAC keys are long-lived secrets (the current multipart signer needs none); the organization policy that disables service account key creation also blocks HMAC keys, and `storage.restrictAuthTypes` can block HMAC requests ([hmackeys](https://docs.cloud.google.com/storage/docs/authentication/hmackeys), [restrict-service-accounts](https://docs.cloud.google.com/organization-policy/restrict-service-accounts)).
- Projects with many GCS users keep a native GCS adapter beside S3 (GitLab, Sentry, Label Studio, DVC); projects relying on the interoperability layer reported multipart, copy, header and precondition breakages (Nextcloud, ClickHouse, Mastodon).

## Object store for the bundle

- MinIO's community edition is no longer maintained: console removed in 2025, no binaries or images since October 2025, repository archived in 2026, `minio/minio` gone from Docker Hub ([minio/minio](https://github.com/minio/minio)).
- SeaweedFS (Apache-2.0, very active, versioning and conditional writes, one container with credentials and bucket from the environment). Versity S3 Gateway (Apache-2.0, objects as plain files on the host filesystem). Garage (AGPL-3.0, smallest and simplest, no versioning or conditional writes, metadata database fragile at one replica). RustFS (Apache-2.0, 1.0 in September 2026, young). Ceph RGW is too heavy for one workstation.

## Python client

- boto3: sync, thread-safe clients, presigns any operation including UploadPart. Since 1.36 it adds streaming checksums that GCS and some S3 clones reject; set `request_checksum_calculation="when_required"` and `response_checksum_validation="when_required"`, path-style addressing, `region_name="auto"` for GCS ([boto3 #4392](https://github.com/boto/boto3/issues/4392), [configuration](https://docs.aws.amazon.com/boto3/latest/guide/configuration.html)).
- obstore cannot presign UploadPart yet; minio-py works but its vendor has stepped back.

## Open questions

The three unverified GCS behaviors above; whether the LBL GCP organization enforces the key-creation or auth-type policies; resource footprints of SeaweedFS and Versity.
