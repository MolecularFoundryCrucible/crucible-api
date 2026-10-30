# S3-compatible storage backends

Status: shipped in 3.3.0. Every byte the API stores goes through one interface, `ObjectStore` in `app/storage/base.py`, with `GoogleStore` (`app/storage/gcs.py`) and `S3Store` (`app/storage/s3.py`, chosen with `STORAGE_BACKEND=s3`); the multipart, download, retirement, and record-file paths are portable and the combined per-part CRC32C check is done. This note records what S3-compatible backends (AWS S3, SeaweedFS, MinIO, Ceph RGW) need from that interface and what is still open.

## GCS-specific pieces

| Area | GCS today | S3-compatible equivalent |
| --- | --- | --- |
| Server credentials | Application Default Credentials, optionally impersonating `MULTIPART_UPLOAD_SIGNER`; URLs signed locally with a key file, or through IAM signBlob | IAM role (AWS) or access keys (MinIO/Ceph) |
| Part URL signing | `blob.generate_signed_url` (V4, IAM signBlob) | boto3 `generate_presigned_url("upload_part", ...)` |
| Per-part checksum header | `x-goog-hash: crc32c=...` | `x-amz-checksum-crc32c` |
| Final object CRC32C | Provider computes a full-object CRC32C | AWS requires the full-object checksum type at initiation; other servers may only return a composite (`-N`) checksum |
| Object version pinning | Object generation | `VersionId` (bucket versioning) or ETag |
| Provider requests | Google `AuthorizedSession` with hand-parsed XML | boto3 SigV4 client |
| Configuration | `STORAGE_BACKEND=gcs`; optional `MULTIPART_UPLOAD_SIGNER` | `STORAGE_BACKEND=s3`, endpoint, region, keys |

Google's S3 interface is not used: it refuses per-part checksums in `CompleteMultipartUpload` and does not check a whole-object checksum there (tested 2026-10-29), so `STORAGE_BACKEND=s3` refuses a Google `S3_ENDPOINT_URL`.

## Open design changes

1. Generalize `storage_generation` into a backend-neutral object version (GCS generation or S3 `VersionId`) across file records, downloads, cleanup, and ingestion messages. The values are already strings; only the column and field names are GCS-specific.
2. Record the backend explicitly instead of inferring it from a bare `bucket/key` `storage_path`.
3. Keep the client contract provider-neutral: clients must send every header returned for a signed part except `Content-Length`, never a hard-coded provider header.

## Outside the multipart flow

The legacy upload routes (`/datasets/{dataset_mfid}/upload/initiate*`, `/upload/complete`; deprecated, retiring in 4.0), the storage path backfill that lists objects under a dataset, and the ingestion consumers still assume GCS. Single-file deletion does not yet remove stored objects at all; see [stored objects left behind by file deletion](file_object_cleanup_followup.md). The multipart flow itself runs inside the API process and needs PostgreSQL row locks for reservations and idempotency.

## Open checks

`python -m app.cli storage check` verifies a target store: presigned `UploadPart` with signed `x-amz-checksum-crc32c` and `Content-Length`, re-uploading a part, refusing a part with the wrong checksum, completion with per-part checksums, and a presigned download with response headers. SeaweedFS 4.51 and Versity S3 Gateway 1.9.0 pass it in CI. SeaweedFS 4.51 lists a part uploaded twice twice; `S3Store` treats identical entries as one part. Lifecycle abort of incomplete uploads and Ceph and MinIO support have not been verified.
