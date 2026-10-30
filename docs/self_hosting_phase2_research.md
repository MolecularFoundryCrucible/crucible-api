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

### Tested 2026-10-29 (Task 7)

`storage check` through `S3Store`, with an HMAC key on a disposable bucket in `mf-crucible`:

- Passed: start, upload a part and re-upload it, refuse a part with the wrong checksum (400), other bytes with the header changed to match (403) or left out (400), list parts (no per-part checksums reported), head (no version IDs; the ETag stands in), signed download with the file name, signed download with `Authorization: Bearer` added (200), record files, empty object, delete, abort.
- Failed: `CompleteMultipartUpload` with a `ChecksumCRC32C` per part, which `S3Store` sends (and AWS requires for uploads started with a checksum algorithm): 400 `MalformedCompleteMultipartUploadRequest`; Google's schema allows only `PartNumber` and `ETag` ([complete](https://docs.cloud.google.com/storage/docs/xml-api/post-object-complete)). Without them completion works and the object's CRC32C matches the combined part checksums, but a wrong whole-object `ChecksumCRC32C` is accepted unchecked.
- Decision (user, 2026-10-29): the Foundry stays on `GoogleStore`, the native adapter; Google's S3 interface is not used, and `STORAGE_BACKEND=s3` refuses a Google endpoint (#391). Clients see the same routes on every backend.
- `GoogleStore` was then made usable by any institution (#391): `GCS_SA` and `MULTIPART_UPLOAD_SIGNER` are optional, the server uses Application Default Credentials, and URLs are signed with a key, by impersonation, or through IAM signBlob. `storage check` passed with the Foundry's settings, impersonation, a key file, and a keyless attached account; the Compose bundle passed on `gcs` with a key file.

## Object store for the bundle

- MinIO's community edition is no longer maintained: console removed in 2025, no binaries or images since October 2025, repository archived in 2026, `minio/minio` gone from Docker Hub ([minio/minio](https://github.com/minio/minio)).
- SeaweedFS (Apache-2.0, very active, versioning and conditional writes, one container with credentials and bucket from the environment). Versity S3 Gateway (Apache-2.0, objects as plain files on the host filesystem). Garage (AGPL-3.0, smallest and simplest, no versioning or conditional writes, metadata database fragile at one replica). RustFS (Apache-2.0, 1.0 in September 2026, young). Ceph RGW is too heavy for one workstation.

## Python client

- boto3: sync, thread-safe clients, presigns any operation including UploadPart. Since 1.36 it adds streaming checksums that GCS and some S3 clones reject; set `request_checksum_calculation="when_required"` and `response_checksum_validation="when_required"`, path-style addressing, `region_name="auto"` for GCS ([boto3 #4392](https://github.com/boto/boto3/issues/4392), [configuration](https://docs.aws.amazon.com/boto3/latest/guide/configuration.html)).
- obstore cannot presign UploadPart yet; minio-py works but its vendor has stepped back.

## Open questions

The three unverified GCS behaviors above; whether the LBL GCP organization enforces the key-creation or auth-type policies; resource footprints of SeaweedFS and Versity.

## Client constraints (nano-crucible session, 2026-10-28, from code at v3.0.0 to v3.3.0 and dev)

- Released clients 3.0.0 to 3.2.1 upload only through the legacy GCS routes (`/upload/initiate/multipart` with a downscoped token and `transfer_manager`, or `/upload/initiate` plus `/upload/complete`). 3.3.0 and dev use the signed multipart flow by default but still reach the legacy routes in four cases: the fallback when initiate answers 503 "multipart uploads are disabled", 0-byte files (the signed flow refuses them client-side), `upload_protocol="legacy"`, and `add_file(multipart=False)`. No released version is signed-only yet; it needs empty-file support in the signed flow (or another way to upload them), and the legacy protocol and fallback removed. So self-hosted installations need nano-crucible 3.3.0 or later, and the Foundry keeps the legacy routes until older clients are gone.
- The signed flow is store-neutral on the client side: part PUTs send whatever headers the API returns, the client never builds `x-goog-*` headers or reads response headers, and its base64 CRC32C values work unchanged with `x-amz-checksum-crc32c`. No client code assumes a storage host name or uses `storage_generation`.
- Downloads break on S3 stores in every released client: the download GET goes through the client's session, which sends `Authorization: Bearer <API key>`, and S3 rejects a presigned URL that also carries an Authorization header (and the API key should not go to the storage host at all). The nano-crucible session will propose a client fix (a session without auth for storage URLs); released versions keep the behavior, so the bundle must either strip the header in front of the store or accept that only fixed clients can download.
- Released clients treat `storage_backend != "gcs"` as a catalog-only record: downloads raise or are skipped, replace refuses, and `storage_path` is expected as `bucket/dataset_mfid/object`. Objects the API stores in an S3 store must therefore keep a managed marker released clients understand (keep `"gcs"` as the historical name for "stored by the API"), or every released client will refuse them.
