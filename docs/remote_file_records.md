# Remote (non-GCS) File Records

Status: Implemented for dataset-associated files. Resource-generic file attachment remains deferred.

## Goal

Let a dataset's `AssociatedFile` entries catalog files that live outside GCS — an HPC partition, a Globus-addressable path, or anywhere else — without Crucible verifying existence or brokering access to them. This is a catalog-only feature: recording where something is and how to get it, not moving bytes.

## Data Model

`AssociatedFile` has two storage-location fields in addition to its existing path:

| Field | Type | Notes |
|---|---|---|
| `storage_backend` | free text, default `"gcs"` | The only value the API treats specially is the literal `"gcs"` — it triggers signed-URL generation. Any other value (`"globus"`, `"nersc-perlmutter"`, `"lawrencium"`, anything) is uniformly "catalog only." No enum, no validation — this lets different HPC clusters be distinguished by value without a schema change, since the API never needs to know *which* non-GCS system it is, only *whether* it's GCS. |
| `storage_path` | unchanged column, meaning broadens | A GCS bucket/blob path when `storage_backend == "gcs"` (as before); a backend-specific location string (Globus URI, HPC filesystem path, etc.) otherwise. Uninterpreted by the API either way. |
| `access_note` | nullable free text | How to actually get the file when there's no self-service path (e.g. "request via NERSC allocation X", "contact PI for Globus share"). |

`sha256_hash`/`size` stay required for every backend — file registration was already client-supplied and unverified server-side even for GCS files (the API never computes a hash from actual bytes), so this introduces no new trust model.

Existing rows were migrated to `storage_backend='gcs'` through the column default, preserving their behavior.

## API Behavior

- `GET /files/{file_id}/download_link` and `GET /datasets/{dsid}/download_links` return a `null` URL for non-GCS files, with **no change to either route's response shape** — a client checks `storage_backend` (via `GET /files/{file_id}` / `GET /datasets/{dsid}/files`) to know what to expect, rather than the download-link routes growing new fields.
- `POST /files/{file_id}/ingest` requires contributor-or-above on the parent dataset and rejects non-GCS files with `400` because ingestion assumes a file is reachable from the GCS FUSE mount, which is not true for catalog-only entries.
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
