# File replacement

This page specifies how the content of a stored file is replaced while its record (MFID, filename and metadata) is kept, and how the previous content is retained and restored. Only records stored by the API can be replaced; catalog-only records are edited with `PATCH /files/{file_id}` ([Remote file records](remote_file_records.md)).

## File ETag

Every file read response (`AssociatedFileRead`) has an `etag` field, and `GET /files/{file_id}` also returns it as the `ETag` header. The value is a quoted strong validator derived from the stored content:

```
etag = '"' + sha256(f"{storage_path or ''}|{storage_generation or ''}|{sha256_hash}|{size}").hexdigest()[:32] + '"'
```

It changes on every replacement and on any change to those four fields, and is stable otherwise. Renaming a file or editing `access_note` does not change it.

## Client flow

1. Obtain the file's `etag` from `GET /files/{file_id}`, a file listing, or the 409 `filename_exists` response of `POST /datasets/{dataset_mfid}/upload/multipart/initiate` or `POST /datasets/{dataset_mfid}/files`, which carries `file_mfid` and `etag`.
2. `POST /files/{file_mfid}/upload/multipart/initiate` with `{"size": <int>, "sha256_hash": <optional>}`, the optional `Idempotency-Key` header, and the optional `If-Match: <etag>` header (`If-Match: *` matches any ETag). The response is a `MultipartStatus` with an `upload_id`.
3. Sign parts, upload, read the status, abort and complete through the dataset's routes under `/datasets/{dataset_mfid}/upload/multipart/` ([Uploads](uploads.md)).
4. Completion returns the `AssociatedFileRead` with the same `mfid` and `filename`, and the new `size`, `sha256_hash`, `storage_path` and `etag`.

After a `filename_exists` conflict, clients ask the user before replacing; they never replace silently. The current content stays downloadable until completion.

## Initiation

`POST /files/{file_mfid}/upload/multipart/initiate` applies these checks in order:

1. The file exists (404 `File not found`) and the caller can read its dataset (403).
2. The caller has the `edit` permission on the dataset (403).
3. The dataset is not pending deletion (409 `pending_deletion`).
4. The file is stored by the API (400 `Only GCS files can be replaced; edit catalog-only records with PATCH /files/{file_id}`).
5. A request with an `Idempotency-Key` already used by the caller for this dataset returns the earlier result, before the `If-Match` comparison. The key's fingerprint includes the replaced file, so reusing a key between an upload and a replacement returns 409.
6. `If-Match`, when present, must match the file's current `etag` (412 `{"detail": {"code": "etag_mismatch", "etag": "<current>", "message": ...}}`).
7. A missing `storage_path` is located as for downloads ([Missing storage paths](storage_downloads.md#missing-storage-paths)). When no object is found, the replacement proceeds with no previous content. A located path is saved even when the request ends at a later step.
8. No other replacement of the file is in progress, that is, no session for it that is neither terminal nor expired (409 `{"detail": {"code": "replace_in_progress", "upload_id": "...", "message": ...}}`).
9. When the file has a `storage_path`, the request's `sha256_hash` and `size` equal the file's, and its hash is trusted (no `storage_generation`, or `sha256_verified_at` set), the route returns state `completed` with `existing_file` and creates no session.
10. Size limits and quotas are those of a new upload. The session records the file and its `etag` after step 7.

## Completion

The upload completion route verifies the object as for a new upload, then publishes it to the existing record:

1. The `edit` permission is checked again. If it has been revoked, the session fails with `permission_revoked` (403 `Upload could not be published: permission_revoked`) and the new object is deleted.
2. The file must still exist, be stored by the API and have the `etag` recorded at initiation. Otherwise the session fails with `file_changed` (409 `Upload could not be published: file_changed`) and the new object is deleted.
3. The record is repointed to the new object: `storage_path`, `storage_generation`, `size`, `sha256_hash` and `storage_crc32c` are set, and `sha256_verified_at` is cleared. The dataset's modification time is updated.

A file in the older layout (`{dataset_mfid}/{filename}`, no generation) moves to the upload layout and from then on behaves like any uploaded file ([Object layouts](storage_downloads.md#object-layouts)). Ingestion requests are not repeated; clients request ingestion again if needed.

## Previous content

Replaced content is never overwritten. On every backend, the previous object is recorded as a row in `file_versions`, in the transaction that repoints the record, and kept for `FILE_VERSION_RETENTION_DAYS` (default 14). `python -m app.cli storage cleanup` then deletes it ([Uploads](uploads.md#operations)); without that command scheduled, replaced content is kept indefinitely.

A version is recorded when the previous object satisfies the [location rule](storage_downloads.md#location-rule) in `UPLOAD_BUCKET` or `MULTIPART_UPLOAD_BUCKET` and has a `storage_generation`. Objects from before multipart uploads have no recorded generation and are left in place. When another file that shared the object was replaced earlier, the existing version's deadline is extended instead. `storage cleanup` deletes versions past their deadline that no record references; a version still referenced is kept, and one outside the deployment's bucket is marked `not_owned` and never deleted.

Every replacement logs `file_replaced` with the file and dataset MFIDs, `previous_path` and `previous_generation`. During the grace period no record points at the previous object and the API does not serve it, but download links issued earlier keep working until they expire.

Earlier releases instead marked replaced objects on Google Cloud Storage with `customTime` for a bucket lifecycle rule (`daysSinceCustomTime`) to delete. Objects marked then are still removed by that rule while the bucket keeps it; it can be dropped once they are gone.

`python -m app.cli files restore FILE_MFID` makes the file's newest kept version current again and records the content it replaces as a version, so running it again undoes the restore. It prints the file MFID, the restored path, the restored version and the newly kept version as JSON. It refuses with `Not restored: <code>`:

| Code | Condition |
|---|---|
| `file_not_found` | No file with this MFID |
| `dataset_pending_deletion` | The dataset has an active deletion request |
| `replace_in_progress` | A replacement of the file is in progress |
| `no_kept_version` | No version is retained |
| `version_expired` | The newest version is past its grace period; older versions are never used instead |
| `version_missing_in_storage` | The version's object no longer exists |
| `version_size_mismatch` | The stored object's size differs from the version's |

## Errors

| Condition | Response |
|---|---|
| File not found / not readable | 404 / 403 |
| No `edit` permission on the dataset | 403 |
| Dataset pending deletion | 409 `pending_deletion` |
| Catalog-only file | 400 |
| `If-Match` mismatch | 412 `etag_mismatch` with the current `etag` |
| Another replacement in progress | 409 `replace_in_progress` with its `upload_id` |
| Same trusted content | 200 `completed` with `existing_file`, no session |
| File changed before completion | 409 `Upload could not be published: file_changed` |
| `edit` permission revoked before completion | 403 `Upload could not be published: permission_revoked` |
| Uploads disabled, size limit, quotas, storage errors | As for new uploads ([Uploads](uploads.md#errors)) |

Aborting a replacement, or letting it expire, leaves the file unchanged and deletes the new object.
