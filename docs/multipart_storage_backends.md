# S3-compatible storage backends

Status: in progress as part of self-hosting (Phase 2, storage). Every byte the API stores now goes through one interface, `ObjectStore` in `app/storage/base.py`, with `GoogleStore` (`app/storage/gcs.py`) as the only implementation; an S3 adapter comes next. This note records what S3-compatible backends (AWS S3, SeaweedFS, MinIO, Ceph RGW) need from that interface.

## What is already portable

- The multipart routes, session state machine, quotas, idempotency, and cleanup depend only on PostgreSQL and `ObjectStore` (`start_upload`, `open_uploads`, `list_parts`, `sign_part`, `finish_upload`, `abort_upload`, `head`, `delete`). Completion passes each part's authorized CRC32C to the store.
- Signed downloads (`signed_download`), retiring a replaced object (`retire`, only on stores with `retires_by_lifecycle`), and the `record.json` and `metadata.json` files (`put_bytes`, `get_text`) go through the same interface. Object versions are strings everywhere (a GCS generation today).
- The GCS XML multipart API mirrors S3 multipart: initiate, upload part, list parts, complete, abort, and list multipart uploads, with the same 10,000-part and 5 MiB minimum part limits. Fixed 64 MiB parts and the 500 GiB cap fit S3 limits.
- Bucket CORS and lifecycle abort of incomplete multipart uploads exist on S3 and MinIO.
- The public `upload_protocol` value `xml-multipart-signed-v1` is provider-neutral.

## GCS-specific pieces

| Area | GCS today | S3-compatible equivalent |
| --- | --- | --- |
| Server credentials | ADC impersonating a keyless signer | IAM role (AWS) or access keys (MinIO/Ceph) |
| Part URL signing | `blob.generate_signed_url` (V4, IAM signBlob) | boto3 `generate_presigned_url("upload_part", ...)` |
| Per-part checksum header | `x-goog-hash: crc32c=...` | `x-amz-checksum-crc32c` |
| Final object CRC32C | Provider computes a full-object CRC32C | AWS requires the full-object checksum type at initiation; other servers may only return a composite (`-N`) checksum |
| Object version pinning | Object generation | `VersionId` (bucket versioning) or ETag |
| Provider requests | Google `AuthorizedSession` with hand-parsed XML | boto3 SigV4 client |
| Configuration | `MULTIPART_UPLOAD_SIGNER`, `MULTIPART_UPLOAD_PROJECT` | Backend selector, endpoint, region, credentials |

## Design changes needed

1. Compute the expected whole-object CRC32C on the server from the authorized per-part CRC32C values and part sizes (CRC32C values can be combined), compare it with the completion claim, and rely on the provider only for per-part checksum enforcement. This removes the dependency on provider-computed full-object checksums.
2. Generalize `storage_generation` into a backend-neutral object version (GCS generation or S3 `VersionId`) across file records, downloads, cleanup, and ingestion messages.
3. Record the backend explicitly instead of inferring it from a bare `bucket/key` `storage_path`.
4. Select the adapter from configuration in `get_store()` (`app/storage/__init__.py`), which returns `GoogleStore` today.
5. Keep the client contract provider-neutral: clients must send every header returned for a signed part except `Content-Length`, never a hard-coded provider header.

## Outside the multipart flow

The legacy upload routes (`/datasets/{dataset_mfid}/upload/initiate*`, `/upload/complete`), the storage path backfill that lists objects under a dataset, and the ingestion consumers still assume GCS. Single-file deletion does not yet remove stored objects at all; see [stored objects left behind by file deletion](file_object_cleanup_followup.md). The multipart flow itself runs inside the API process and needs PostgreSQL row locks for reservations and idempotency.

## Open checks

Verify per target backend: presigned `UploadPart` with a signed `x-amz-checksum-crc32c` header, full-object or composite checksum behavior on completion, conditional or version-pinned deletes, and lifecycle abort support. Current MinIO and Ceph support for these has not been verified.
