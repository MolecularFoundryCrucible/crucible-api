# Remote (not stored by the API) File Records

Status: Implemented for dataset-associated files. Resource-generic file attachment remains deferred.

## Goal

Let a dataset's `AssociatedFile` entries catalog files that live outside the API's storage - an HPC partition, a Globus-addressable path, or anywhere else - without Crucible verifying existence or brokering access to them. This is a catalog-only feature: recording where something is and how to get it, not moving bytes.

## Data Model

`AssociatedFile` has two storage-location fields in addition to its existing path:

| Field | Type | Notes |
|---|---|---|
| `storage_backend` | free text, default `"gcs"` | The only value the API treats specially is the literal `"gcs"` - it triggers signed-URL generation. Any other value (`"globus"`, `"nersc-perlmutter"`, `"lawrencium"`, anything) is uniformly "catalog only." No enum, no validation - this lets different HPC clusters be distinguished by value without a schema change, since the API never needs to know *which* external system it is, only *whether* the API stores the file. |
| `storage_path` | unchanged column, meaning broadens | A GCS bucket/blob path when `storage_backend == "gcs"` (as before); a backend-specific location string (Globus URI, HPC filesystem path, etc.) otherwise. Uninterpreted by the API either way. |
| `access_note` | nullable free text | How to actually get the file when there's no self-service path (e.g. "request via NERSC allocation X", "contact PI for Globus share"). |

`sha256_hash`/`size` stay required for every backend — file registration was already client-supplied and unverified server-side even for GCS files (the API never computes a hash from actual bytes), so this introduces no new trust model.

Existing rows were migrated to `storage_backend='gcs'` through the column default, preserving their behavior.

Despite its name, `"gcs"` means "stored by the API", in whichever store the server is configured with (`STORAGE_BACKEND`, Google Cloud Storage or S3-compatible). The value is kept for compatibility with existing records and clients; it is not rewritten when a server uses S3.

## Identity and filenames

A file's identity in its dataset is its `filename` (from PR #335 by Peter Doak, extended to every backend):

- `POST /datasets/{dataset_mfid}/files` with an existing filename returns the existing record when `sha256_hash`, `size`, and `storage_backend` match (`storage_path` and `access_note` are not compared and are not updated), and `409 filename_exists` otherwise. Identical content under different filenames gets separate records, so a dataset like `freq_001/kpoints.json`, `freq_002/kpoints.json` keeps every file.
- Non-GCS filenames must be canonical relative POSIX paths: not empty, no leading, trailing, or repeated `/`, no `.` or `..` components, no ASCII control characters (U+0001 to U+001F, U+007F). Invalid names return `422`; nothing is normalized. Clients rebuild datasets on disk from these paths.
- The database enforces both for non-GCS rows: check constraints on the path and a partial unique index on `(dataset_id, filename)`, which also settles two concurrent posts of the same name (migration `b8f2d6e41c07`).
- `PATCH /files/{file_id}` cannot change a non-GCS filename (`409 filename_immutable`), cannot move a record between stored by the API and not (`409 storage_backend_class_change`), and cannot rename a GCS file onto a filename already used in the dataset (`409 filename_exists`).

GCS filenames keep their legacy shapes: many production records store the original instrument path (for example `/mnt/gcs/...` or `/home/...`) and some consumer-generated files start with `./`. The path rules and the database index apply to GCS only after those and the duplicate records are resolved (`file_name_uniqueness_followup.md`).

## API Behavior

- `GET /files/{file_id}/download_link` and `GET /datasets/{dataset_mfid}/download_links` return a `null` URL for files not stored by the API, with **no change to either route's response shape** - a client checks `storage_backend` (via `GET /files/{file_id}` / `GET /datasets/{dataset_mfid}/files`) to know what to expect, rather than the download-link routes growing new fields.
- `POST /files/{file_id}/ingest` requires contributor-or-above on the parent dataset and rejects files not stored by the API with `400` because ingestion assumes a file is reachable from the GCS FUSE mount, which is not true for catalog-only entries.
- Remote and GCS file entries use the same ACL evaluator. Reading or downloading requires viewer-or-above, updating requires editor-or-above, requesting ingestion requires contributor-or-above, and deleting requires admin-or-above on the parent dataset.

## What This Doesn't Do (and why)

**No active transfer/broker integration.** Crucible doesn't call the Globus Transfer API or move bytes on a client's behalf — it only records a location and access instructions. Building active brokering would mean holding credentials for arbitrary remote systems, tracking transfer jobs, and handling a whole new class of failure modes; nothing today requires it, and it's a meaningfully bigger project than cataloging.

**No structured Globus fields** (endpoint UUID + path as first-class columns). Not every remote file will be Globus-addressable, and since there's no active transfer to power, there's no functional reason to parse Globus URIs yet. If active Globus integration is ever built, structured fields can be derived from the existing free-text `storage_path` values at that point rather than designed speculatively now.

**Samples and instruments still can't have attached files.** `AssociatedFile.dataset_id` remains a dataset-specific foreign key. See "Future: generalizing file attachment" below.

## Future: generalizing file attachment

`AssociatedFile.dataset_id` is a hard `int` FK to `dataset.id` — unlike `ResourceACL.mfid`, `ScientificMetadataBinary.unique_id`, and `DeletionRequest.resource_id`, which all key off the generic `idtype.mfid` layer and so work across any resource type. If `AssociatedFile` were changed to a generic `resource_id` (mfid) FK, samples and instruments could attach files with no new table or route family — the same pattern already used for ACLs, metadata, and deletion requests.

This wasn't done as part of the remote-file-records work because:
- It's a real migration, not a rename — `dataset_id` (int, references `dataset.id`) would need to become `resource_id` (string, references `idtype.mfid`), requiring a backfill and touching every query that currently joins on `AssociatedFile.dataset_id == Dataset.id`.
- There's no current use case — samples and instruments don't need file attachments today.
- It trades a small, likely-unmeasurable amount of index efficiency (26-char mfid string keys vs. 4-byte int keys) for genericity — the same tradeoff `ResourceACL` and friends already made, and one that hasn't caused a measurable problem there.

Revisit this if/when a non-dataset resource actually needs to attach files.

## Future: file-identifier naming

The `/files/{file_id}` path segments and file response fields (`AssociatedFileRead.mfid`, `dataset_id`) predate the `_mfid` naming convention: `file_id` values hold MFIDs, and `AssociatedFileRead` exposes the canonical MFID under `mfid` rather than `file_mfid`. Renaming the path segments and adding a `file_mfid` response field is deferred.

`GET /ingestion_requests` already accepts `file_mfid` (with `file_id` as a deprecated alias), so dataset-side queries can use the convention now. The route rename should land in the same coordinated step that renames the client's `file_id` parameters, so clients do not carry a mixed naming surface. Coordinate with the file-attachment generalization above, which also touches these route shapes.
