# Replacing a file's content

## Status

Implemented on branch feat/file-replace (2026-10-13). Builds on storage location confinement ([design](storage_download_design.md)) and one upload bucket per environment (`UPLOAD_BUCKET`).

## Problem

There is no API route that changes a file's bytes while keeping the file record. Today a client can only re-upload through a legacy route, which overwrites the object in place and adds a duplicate record whose siblings keep a stale hash, or delete and re-upload, which needs DELETE, changes the file MFID, and leaves the old object in the bucket. The new multipart flow refuses an existing filename with a plain 409, and `PATCH /files/{file_id}` can no longer repoint a GCS file's bytes.

## Goals

- Replace the content of an existing GCS file through the multipart upload flow, keeping its MFID, filename, and metadata.
- Never overwrite stored bytes in place: the new content is a new immutable object, and the record is repointed atomically.
- Keep the old object recoverable for a grace period, then remove it without a background worker.
- Detect concurrent changes instead of silently overwriting them.
- Tell clients that a filename already exists in a form they can act on.

## Non-goals

- Replacing catalog-only (non-GCS) records. Their metadata, including `storage_path`, stays editable through `PATCH /files/{file_id}`.
- A single-request replace route. All new uploads, including replacements, use the multipart flow.
- Changes to legacy upload routes, which keep their current behavior.
- Ingestion changes. Existing ingestion requests are left as they are; clients request ingestion again after a replace if they want it.
- An audit table or version history API.

## Established practice

Major file APIs address a replacement to the existing file and guard it with optimistic concurrency: Microsoft Graph `POST /drive/items/{itemId}/createUploadSession` with `If-Match`, Box `POST /files/{file_id}/content` with `If-Match`, Google Drive `PATCH /upload/drive/v3/files/{fileId}`, and Dropbox upload `mode=update` with `rev`. A mismatched precondition returns 412. Object storage practice is one immutable object per upload, with the record pointing at the current one ([Active Storage blobs](https://api.rubyonrails.org/classes/ActiveStorage/Blob.html)).

## File ETag

Every file read response (`AssociatedFileRead`) gains an `etag` field, and `GET /files/{file_id}` also returns it as the `ETag` header. The value is a quoted strong validator derived from the stored content identity:

```
etag = '"' + sha256(f"{storage_path or ''}|{storage_generation or ''}|{sha256_hash}|{size}").hexdigest()[:32] + '"'
```

It changes on every replace and on any change to those four fields, and is stable otherwise. Renaming a file or editing `access_note` does not change it.

## Client flow

1. Learn the file's `etag` from `GET /files/{file_id}`, any file listing, or a `filename_exists` 409 (below).
2. `POST /files/{file_mfid}/upload/multipart/initiate` with body `{"size": <int>, "sha256_hash": <optional>}`, the optional `Idempotency-Key` header, and the optional `If-Match: <etag>` header (`If-Match: *` matches any current ETag). The response is the existing `MultipartStatus` with an `upload_id`.
3. Sign parts, upload, read status, abort, and complete through the existing `/datasets/{dataset_mfid}/upload/multipart/...` routes, using the file's dataset. Nothing about those routes changes for clients.
4. Completion returns the same `AssociatedFileRead`: same `mfid` and `filename`, new `size`, `sha256_hash`, `storage_path`, and `etag`.

Discovering a conflict on a new upload: `POST /datasets/{dataset_mfid}/upload/multipart/initiate` with a filename that already exists returns 409 `{"detail": {"code": "filename_exists", "message": "Filename already exists", "file_mfid": "...", "etag": "..."}}`. Clients ask the user before replacing; they never replace silently.

## Initiation

`POST /files/{file_mfid}/upload/multipart/initiate`. The dataset row is locked first, then the file row, the same order completion uses.

1. The file exists and the caller can read its dataset (404 or 403 as for other file routes). A file whose dataset row is gone by the time it is locked is reported as 404 `File not found`.
2. The caller has EDIT on the dataset (403).
3. `storage_backend` is `gcs` (400 `Only GCS files can be replaced; edit catalog-only records with PATCH /files/{file_id}`).
4. Idempotent replay: a request with an `Idempotency-Key` already used by this caller for this dataset returns the earlier result exactly as for a new multipart upload, before the `If-Match` comparison and before any backfill, so a retry with the same key and `If-Match` succeeds even when the first call backfilled the path and changed the ETag. The idempotency fingerprint includes the replace target, so reusing a key between a create and a replace returns 409 as any changed request does.
5. `If-Match`, when present, equals the `etag` of the file record as currently stored, before any backfill (412 `{"detail": {"code": "etag_mismatch", "etag": "<current>"}}`). `If-Match: *` matches any current ETag.
6. A missing `storage_path` is backfilled from the dataset prefix as downloads do, listing the prefix with a 30-second timeout; if no object is found, the replace proceeds with no old object to retire. The backfilled path is persisted even when the request returns early below (a live session or trusted content), because the record is now accurate regardless of what this request does next.
7. No live replace session exists for this file (409 `{"detail": {"code": "replace_in_progress", "upload_id": "..."}}`). A session counts as live under the same rule as filename reservations: multipart protocol, non-terminal state, not expired. The check runs with the file row locked.
8. If the file has a `storage_path` after the backfill attempt, `sha256_hash` and `size` equal the file's current values, and its hash is trusted (`multipart.trusted_hash()`), return `MultipartStatus(state="completed", existing_file=<file>)` without creating a session. A file that is still pathless never short-circuits, because its bytes are not known to exist.
9. Size limits, quotas, and the reservation path are those of a new multipart upload. The session is created with `associated_file_name` = the file's filename, `replace_file_mfid` = the file MFID, and `replace_base_etag` = the etag after backfill. The object key is `{dataset_mfid}/_files/{upload_id}` as for any multipart upload.

The current file stays downloadable throughout the upload.

## Completion

The existing completion path verifies and publishes the object as for a new upload. For a session with `replace_file_mfid`, publication differs:

1. Lock the dataset and session rows as today, then lock the file row.
2. Re-check EDIT on the dataset; if it has been revoked, fail the session with `permission_revoked` (403 `Upload could not be published: permission_revoked`) and clean up the new object, as other publication failures do.
3. The file still exists, its `storage_backend` is still `gcs`, and its current `etag` equals `replace_base_etag`; otherwise fail the session with `file_changed` (409 `Upload could not be published: file_changed`) and clean up the new object. A file whose backend changed away from `gcs` between initiation and completion counts as changed even if its other stored fields still match.
4. Repoint the file: `storage_path` = `{bucket}/{dataset_mfid}/_files/{upload_id}`, `storage_generation`, `size`, `sha256_hash`, `storage_crc32c`, and `sha256_verified_at` = null. Record the previous `storage_path` and generation for step 6. Touch the dataset modification time. Set the session's `result_file_mfid` to the file MFID and mark it completed. Commit.
5. Return the updated file.
6. After the commit, retire the previous object unless another file record still references the same `storage_path`.

A replaced legacy file (`{dataset_mfid}/{filename}`, no generation) ends up in the multipart layout with a generation, and from then on behaves like any multipart file, including generation-pinned downloads and the `PATCH` protections for published multipart records.

## Retiring the old object

Retiring sets the old object's `customTime` to the completion time instead of deleting it. A bucket lifecycle rule `{"action": {"type": "Delete"}, "condition": {"daysSinceCustomTime": 7}}` removes it after seven days with no API worker. Nothing else in the API sets `customTime`, so the rule affects only retired objects.

The old object is retired only when all of the following hold; otherwise it is left in place and logged with `retired=False`:

- It is in the environment's own bucket (`UPLOAD_BUCKET` or the multipart upload bucket), never a `STORAGE_READ_BUCKETS` bucket. On staging, legacy files that live in `mf-storage-prod` are left in place.
- It passes the storage location rule (bucket allow-listed, key under the dataset, not an unpublished multipart object).
- No other `AssociatedFile` record references the same `storage_path`.
- For a legacy object with no generation: its key is exactly `{dataset_mfid}/{filename}`; no pathless record in the dataset could backfill to that key, that is, no record with `storage_path` null has a filename whose basename (`PurePosixPath(filename).name`) is a suffix of the key, the same match `_determine_storage_path` uses; and no unexpired legacy upload session (`protocol` null) in the dataset targets the same key.

The update uses the `GCS_SA` storage client that also signs downloads, with a 30-second timeout on both the generation read and the patch. For a legacy object the generation is read immediately before the patch and the patch is pinned to it with `if_generation_match`; for a multipart object the generation already recorded on the file is used directly. Both calls use the storage client's default retry bounded to 30 seconds. A legacy upload pending for the same key blocks retirement (rule above). The pin only protects the gap between reading the generation and patching: an overwrite of the same key, or a new record landing on the same `storage_path`, racing the stamp itself is not caught, so that case remains best effort.

During the grace period the old object is not served by the API, because no record points at it, but download links issued before the replace keep working until they expire, and an administrator can restore the file by repointing the record. The previous path and generation are written to the application log with the file and dataset MFIDs on every successful replace, whether retirement succeeds, is skipped by the rules above, or fails, and also when the file had no previous object (`previous_path=None`, `retired=False`).

After the grace period, the native safety nets still apply: staging's 7-day soft delete, and on production the bucket's versioning keeps the deleted version for its existing 30-day noncurrent-version rule.

If retiring fails (storage unavailable, generation mismatch, or any error in the eligibility checks, database queries included) or is skipped by the eligibility rules above, the outcome is logged and the object stays in place; the committed replace response is never affected. The file itself is already consistent; the cost is an orphaned object.

## Schema

`upload_sessions` gains two nullable columns, `replace_file_mfid` (text, indexed) and `replace_base_etag` (text), with an Alembic revision chained after the current head. No data migration. Existing sessions have nulls and behave as before. `EXPECTED_DB_REVISIONS` in both configmaps moves to the new revision, so the migration must run on each environment's database before its deploy; running it requires the user's explicit authorization.

## Errors

| Condition | Response |
|---|---|
| File not found / not readable | 404 / 403 |
| No EDIT on the dataset | 403 |
| Catalog-only file | 400 |
| `If-Match` mismatch (`*` always matches) | 412 `etag_mismatch` with the current `etag` |
| Another live replace for the file | 409 `replace_in_progress` with its `upload_id` |
| Same trusted content | 200 `completed` with `existing_file`, no session |
| File changed before completion, including its backend no longer being `gcs` | 409 `Upload could not be published: file_changed` |
| EDIT revoked before completion | 403 `Upload could not be published: permission_revoked` |
| Multipart disabled, quotas, storage errors | As for new multipart uploads (503, 429, 413) |

## Infrastructure

- `mf-storage-staging`: add the `daysSinceCustomTime: 7` delete rule next to the existing abort-incomplete-multipart rule.
- `mf-storage-prod`: add the same rule when multipart is enabled there, checking it against the bucket's versioning rules. This joins the production multipart checklist (prod signer, CORS PUT for the Hub origin, abort-incomplete-multipart rule, ingestion consumers).

## Testing

- ETag: present on file reads and in the `GET /files/{file_id}` header; stable across rename; changes when content fields change.
- Initiation: EDIT required; catalog-only 400; `If-Match` match, mismatch, and `*`; idempotent replay with the same `If-Match` after a backfill; dataset gone is 404; second live replace 409 and an expired one not counted; same trusted content short-circuits, but not for a file still pathless after the backfill; missing path backfilled with a bounded listing.
- Completion: file repointed with the same MFID and filename; legacy file moves to the `_files` layout and gains a generation; `file_changed` when the file was replaced, edited, or its backend changed away from `gcs` in between; EDIT revoked before completion fails publication with `permission_revoked`.
- Retirement: previous object gets `customTime` pinned to its generation; skipped when the object is outside the environment's own bucket, fails the storage location rule, when another record shares the path, when a legacy key doesn't match `{dataset_mfid}/{filename}`, a pathless record's basename is a suffix of the key, or an unexpired legacy upload targets the key, or when the file had no object (still logged); logged and non-fatal on storage or database failure.
- New-upload 409 is structured `filename_exists` with `file_mfid` and `etag`.
- Abort and expiry of a replace session leave the file untouched and clean up the new object.

## Rollout

Deploy to staging, add the staging lifecycle rule, then replace one legacy file and one multipart file through nano and Hub, checking downloads of the new content, the old object's `customTime`, and the 409/412 paths. Clients add "replace" on `filename_exists` with explicit user confirmation.
