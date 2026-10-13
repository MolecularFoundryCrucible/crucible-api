# Storage location confinement and unified downloads

## Status

Implemented on branch fix/storage-path-confinement (2026-10-13). Step 2 of the storage work that follows the downscoped legacy upload token (#310). Per-environment buckets and file replacement build on it.

## Problem

`GET /files/{file_id}/download_link`, `GET /datasets/{dataset_mfid}/download_links`, and ingestion dispatch trust `AssociatedFile.storage_path` as written. They split it into bucket and object key and act on it with the `GCS_SA` identity, which holds project `roles/owner`.

`POST /datasets/{dataset_mfid}/files` already rejects a client `storage_path` for GCS files, but `PATCH /files/{file_id}` does not. A caller with `EDIT` on one dataset can set any `storage_path`, or create a catalog-only record and switch its backend to `gcs`, then obtain a signed URL for any object in any bucket of the project, including other datasets and unrelated buckets. Ingestion forwards the same path to workers.

Downloads also follow two code paths chosen by bucket name: the multipart bucket signs with the dedicated signer and requires a generation, every other bucket signs with `GCS_SA`. Once uploads share one bucket per environment (step 3), bucket name can no longer tell the two apart.

## Audit of existing records

`migrations/data/20261013_audit_file_storage_paths.py`, read-only, run on 2026-10-13:

| Placement | Staging | Production |
|---|---|---|
| Records | 6,003 | 245,506 |
| Own dataset prefix, `mf-storage-prod` | 5,750 | 235,740 |
| Own dataset prefix with an empty segment (`{dsid}//name`) | 0 | 3,769 |
| Own dataset multipart prefix, generation recorded | 229 | 0 |
| No `storage_path` | 24 | 5,997 |
| Another dataset, another bucket, dot segments, leading slash | 0 | 0 |
| Objects shared by several records | 0 | 102 objects, 244 records, all within one dataset |

Every existing record satisfies the rule below. The audit reflects current values only; a path changed and restored leaves no trace in the database.

## Goals

- One function decides whether a file's object may be served and produces its signed URL; every download and ingestion path uses it.
- An object is served only from an allowed bucket and only under the file's own dataset.
- Clients can no longer choose the storage location of a GCS file.
- Multipart objects live under their dataset with server-generated, immutable names, out of reach of legacy upload tokens.
- Existing records, clients, and download behavior keep working.

## Non-goals

- Per-environment buckets and the `UPLOAD_BUCKET` setting (step 3).
- Moving signing to a keyless dedicated signer for every file (step 3, when bucket and IAM configuration change anyway).
- File replacement.
- Cleaning up the 244 shared-object records or the empty-segment keys.

## Object layout

Established practice for user uploads is a server-generated object name with the original filename kept only in the database ([OWASP File Upload Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/File_Upload_Cheat_Sheet.html), [Active Storage blobs](https://api.rubyonrails.org/classes/ActiveStorage/Blob.html)), one immutable object per upload instead of overwriting a key, and the tenant identifier in the key prefix so credentials, lifecycle rules, and cleanup can be scoped by prefix ([AWS multi-tenant S3 patterns](https://aws.amazon.com/blogs/storage/design-patterns-for-multi-tenant-access-control-on-amazon-s3)).

| Layout | Key | Written by |
|---|---|---|
| Legacy | `{dataset_mfid}/{filename}` | `/upload/initiate`, `/upload/initiate/multipart`, and the clients holding their tokens |
| Multipart | `{dataset_mfid}/_files/{upload_id}` | `/upload/multipart/...`, server-generated, never overwritten |

The legacy layout is kept unchanged for compatibility, including re-uploads that overwrite an object in place. The multipart layout replaces `uploads/{dataset_mfid}/{upload_id}/content`, which never reached production. The 229 staging test files under the old layout fail the location rule after this change and are left as test data.

The reserved `_files` segment separates the two layouts structurally:

- The downscoped legacy token allows `{dataset_mfid}/` except `{dataset_mfid}/_files/`, so legacy token holders cannot overwrite or delete multipart objects.
- Legacy upload initiation rejects a filename whose first segment is `_files` with 400, so a legacy upload cannot collide with a multipart object. No existing record uses the segment.
- Code identifies a multipart object by prefix, without inspecting the identifier format.

Keys are not named by file MFID: a future file replacement writes a new object and repoints the record, and the old object is deleted.

## Location rule

A GCS file's `storage_path` is `{bucket}/{key}`. It is servable when:

1. `bucket` is an allowed read bucket: `mf-storage-prod` (the current legacy bucket constant) or the configured `MULTIPART_UPLOAD_BUCKET`.
2. `key` starts with `{dataset_mfid}/`, where `dataset_mfid` is the dataset that owns the record.
3. `key` has no `.` or `..` segment and no leading `/`. Empty segments are allowed because 3,769 production keys contain them and GCS treats them literally.
4. A multipart key (`{dataset_mfid}/_files/...`) has a recorded `storage_generation`. Without one the object is not a published multipart file.

The rule is a pure function, `resolve_storage_object(storage_path, generation, dataset_mfid) -> StorageObject(bucket, key, generation)`, raising a domain error that routes map to HTTP responses.

## Unified download

`signed_download_url(client, obj, filename, disposition="attachment") -> url` replaces the bucket-based branch in `_generate_signed_url_link`. The route helper `_signed_download(file_mfid, dataset_mfid, storage_path, generation, filename, disposition="attachment") -> (url, expires_in)` calls `resolve_storage_object` and maps its errors to HTTP before calling `signed_download_url`:

1. `resolve_storage_object` (the rule above).
2. Load object metadata, pinned to the generation when one is recorded. A missing object is 404, as today.
3. Response headers, set through V4 signed URL response overrides, chosen by the caller's `disposition` query parameter (`attachment` by default, `inline` opt-in; any other value is rejected with 422):
   - `attachment`: `Content-Disposition: attachment` with `filename*=UTF-8''<encoded basename>` and an ASCII fallback, both from the record filename. The stored content type is served unchanged (no `response-content-type` override).
   - `inline`: `Content-Disposition: inline` with the same filename, and `response-content-type` guessed from the record's `filename` when recognized; when no guess exists, the stored object type is left unchanged. Script-capable types are still forced to `attachment` regardless of the request: `text/html`, `application/xhtml+xml`, `image/svg+xml`, `text/xml`, `application/xml`, `text/xsl`, or any type ending in `+xml`.
4. Sign a V4 GET URL valid for one hour, pinned to the generation when recorded, with the existing `GCS_SA` credentials. The multipart signer stays dedicated to upload operations in this step.

`GET /files/{file_id}/download_link` and `GET /datasets/{dataset_mfid}/download_links` both accept `?disposition=attachment|inline`. The Hub pattern: a download button omits the parameter (saves with the record name, original type preserved), a filename link uses `?disposition=inline` (opens in the browser when the type allows it).

Effect on current behavior:

| Files | Today (before this change) | Default (`attachment`) | Opt-in (`inline`) |
|---|---|---|---|
| Legacy | Download (stored as octet-stream), name from URL | Download, name from record, stored type unchanged | Recognized types open inline, others download; name from record |
| Multipart | Attachment, name from record | Download, name from record, stored type unchanged | Inline or download by type, name from record |
| HTML, SVG, XML, XSL | Rendered inline from `storage.googleapis.com` | Attachment | Attachment (forced) |

The default keeps legacy files downloading exactly as before, and now also makes multipart files save with the record name instead of a guessed type. Programmatic clients ignore the disposition header and are unaffected by either mode.

## Missing storage paths

`_ensure_storage_path` keeps backfilling a missing `storage_path` by listing `{dataset_mfid}/` in `mf-storage-prod` and matching the basename. The backfilled value is stored by the download route before signing; the location rule is applied when the URL is signed, and before an ingestion request is created.

## Writes

`PATCH /files/{file_id}` validates the resulting record, not only the request body:

- When the resulting backend is `gcs`, `storage_path` may be omitted, left unchanged, or cleared to null (the next download backfills it from the dataset's own prefix). Setting a new value, or switching a record with a path to `gcs`, returns 422 `storage_path is assigned by the upload API for GCS files`, matching the create-time message.
- When the resulting backend is `gcs`, changing `size` or `sha256_hash` returns 422 `size and sha256_hash are determined by the stored object for GCS files`. Both describe the stored bytes, and hand edits are how records drift from them. Content changes only through an upload.
- A record with a recorded `storage_generation` (a published multipart object) cannot have its `storage_path` cleared or its backend switched away from `gcs`; both return 422 `storage_path is assigned by the upload API for GCS files`. Records without a generation keep the behavior above.
- Non-GCS records keep free-form `storage_path`, `size`, and `sha256_hash`; they are never signed or ingested.

Known clients are unaffected: nano sets `storage_path` only for non-GCS backends, Hub never sets it, and ingestion workers do not write it.

## Ingestion

`POST /files/{file_id}/ingest` and the deprecated dataset-scoped route resolve the object through `resolve_storage_object` before creating the request. A record that fails the rule returns 409 with the rule that failed, and no request is created or dispatched. This replaces the bucket-name generation check in `app/api/routes/files.py`.

## Errors

| Condition | Single download | Bulk download links | Ingestion |
|---|---|---|---|
| Non-GCS backend | `url: null` (unchanged) | `null` (unchanged) | 400 (unchanged) |
| Location rule fails | 409 | `null` for that file | 409 |
| Object missing | 404 (unchanged) | `null` (unchanged) | n/a |
| Signing fails | 503 (unchanged) | `null` (unchanged) | n/a |

A rule failure returns `{"detail": {"code": "storage_location_invalid", "reason": "<reason>", "message": "File storage location is not valid: <reason>"}}`, the same structured shape as the API's other 409 conflicts, so clients key on `code` rather than the message. Rule failures are logged with file and dataset MFIDs only.

## Testing

- Layout: new multipart sessions use `{dataset_mfid}/_files/{upload_id}`; legacy initiation rejects `_files/...` filenames; the token condition contains the exclusion.
- Rule: own legacy prefix, own multipart prefix with and without generation, old `uploads/` layout rejected, another dataset, another bucket, sibling prefix (`{dsid}x/`), dot segments, leading slash, empty segments allowed.
- Headers: default attachment with no type override, inline with the guessed type for a PDF, attachment for HTML and SVG under both defaults, non-ASCII filename encoding, unknown type keeps the stored type under inline, 422 for an unrecognized `disposition` value.
- Generation pinning reaches the signer and the metadata lookup.
- `PATCH /files`: new GCS path rejected, backend switch to `gcs` with a path rejected, unchanged or cleared path accepted, GCS `size` and `sha256_hash` changes rejected, non-GCS free-form edits accepted.
- Bulk links return `null` for a rule failure without failing the whole response.
- Ingestion refuses a rule failure before creating the request.

## Multipart service and legacy token changes

- `app/services/multipart.py` writes new sessions to `{dataset_mfid}/_files/{upload_id}`. Cleanup, publication, and orphan checks use the stored session key, so sessions created before the change complete under their original key.
- `_mint_upload_token` adds the exclusion to its availability condition: `startsWith('.../objects/{dataset_mfid}/') && !startsWith('.../objects/{dataset_mfid}/_files/')`. The disposable-bucket probe is extended with a `_files/` read, overwrite, and delete case, all expected to return 403, before release.
- Legacy initiation (`/upload/initiate` and `/upload/initiate/multipart`) rejects filenames whose first segment is `_files`.

## Rollout

No migration and no configuration change. Deploy to staging, re-run the audit, and download a legacy file and a multipart file through Hub and nano. The audit script stays in `migrations/data/` to re-run before production. A multipart session already in flight on staging at deploy completes under its old `uploads/...` key and then fails the location rule (409); the affected sessions are test data and accepted as-is.

## Follow-ups

- Step 3: per-environment buckets, `UPLOAD_BUCKET`, and one keyless signer for all downloads.
- Check whether GCS data-access logs are enabled on `mf-storage-prod` to look for past cross-dataset reads.
- Deduplicate the 102 shared objects once file replacement exists.
