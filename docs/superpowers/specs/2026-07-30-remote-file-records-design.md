# Remote (non-GCS) File Records — Design

## Overview & scope

Extend the existing `AssociatedFile` model so a dataset's attached files can
reference storage outside GCS — an HPC partition, a Globus-addressable path,
or anywhere else — as a **catalog entry only**. Crucible doesn't broker
access, verify existence, or transfer bytes for non-GCS files; it just
records where something is and enough context to find or request it.

Explicitly in scope:
- Datasets only (`AssociatedFile.dataset_id` stays as-is, unchanged FK).
- Catalog/metadata tracking of a remote file's location and access
  instructions.
- Keeping every existing GCS-backed file flow byte-for-byte unchanged.

Explicitly out of scope (see "Future considerations" below):
- Active transfer/broker integration (e.g. calling the Globus Transfer API).
- Samples/instruments having attached files.
- Generalizing `AssociatedFile.dataset_id` to a resource-generic FK.
- Constrained/validated `storage_backend` values.

## Background: what's true today

- `AssociatedFile.storage_path` is a free-text column, documented as "full
  GCS path" but not format-enforced.
- `POST /datasets/{dsid}/files` (`AssociatedFileCreate`) already requires
  only client-supplied `filename`, `size`, `sha256_hash` — no server-side
  verification against actual bytes at creation time. `sha256_hash` is never
  computed server-side anywhere in this codebase (grepped for `checkhash()`,
  its only call site is its own definition) — verification, when it
  happens, is done out-of-band by separate audit tooling. This means
  requiring `sha256_hash` for a remote file introduces no new trust model;
  it's exactly as (un)verified as it already is for GCS files.
- The only place actually coupled to GCS mechanics is
  `app/api/routes/gcs_storage.py`'s `_ensure_storage_path` /
  `_generate_signed_url_link`, called from the two download-link routes.
  Everything else (creation, listing, ACL checks, `modification_time`
  bumping) is already backend-agnostic.
- No existing concept of storage backend, external reference, remote URI,
  or Globus integration exists anywhere in this codebase today — this is
  greenfield.

## Data model changes

On `AssociatedFile`:

| Field | Change | Notes |
|---|---|---|
| `storage_backend` | **new**, `str`, default `"gcs"` | Free text. The API treats the literal value `"gcs"` specially (triggers signed-URL logic); any other value is treated uniformly as "catalog only" — no enum, no validation against a known list of systems. Existing rows backfill to `"gcs"` via the column default; zero behavior change for existing data. |
| `storage_path` | unchanged column, meaning broadens | GCS bucket/blob path when `storage_backend == "gcs"` (as today); a backend-specific location string (Globus URI, HPC filesystem path, anything) otherwise. Uninterpreted by the API either way — no parsing, no validation. |
| `access_note` | **new**, nullable `str` | Free text for how to actually get the file when there's no self-service path (e.g. "request via NERSC allocation X", "contact PI for Globus share"). |
| `sha256_hash`, `size` | unchanged, still required | Same trust model as today on both backends. |

### Why not a separate `external_location` field, or structured Globus fields

Considered and rejected during design:
- **Separate `external_location` column alongside `storage_path`**: would
  create an unenforced invariant ("exactly one of these two is populated,
  depending on `storage_backend`") for no benefit, since `storage_backend`
  already has to be checked before the location can be interpreted either
  way. One field, one meaning gated by a discriminator, is simpler.
- **Structured Globus fields** (endpoint UUID + path as first-class
  columns): premature — not every remote file will be Globus-addressable,
  and since this design is catalog-only (no active transfer), there's no
  functional reason to parse/validate Globus-specific structure yet. If
  active Globus integration is ever built, structured fields can be derived
  from the existing free-text `storage_path` at that point.
- **Constrained `storage_backend` enum** (e.g. `gcs` / `globus` /
  `external`): rejected in favor of free text specifically so different HPC
  clusters (NERSC, Frontier, Lawrencium, ...) can be distinguished by value
  without a schema change — the API only ever needs to distinguish `"gcs"`
  from "not gcs," so anything beyond that reserved value is pure,
  unconstrained descriptive data.

## API / route changes

- **`AssociatedFileCreate`** gains `storage_backend: str = "gcs"`
  and `access_note: Optional[str] = None`. `POST /datasets/{dsid}/files`
  unchanged otherwise — dedup-by-hash, ACL checks, `modification_time`
  bump all apply identically regardless of backend.
- **`AssociatedFileUpdate`** gains the same two fields alongside the
  existing `storage_path`, so a catalog entry can be corrected/enriched
  after creation.
- **`AssociatedFileRead`** gains `storage_backend` and `access_note` (plus
  the already-present `storage_path`) — this is where catalog info is
  actually discoverable, via `GET /files/{file_id}` and
  `GET /datasets/{dsid}/files`.
- **`GET /files/{file_id}/download_link`** and
  **`GET /datasets/{dsid}/download_links`**: add one guard each — only call
  `_ensure_storage_path`/`_generate_signed_url_link` when
  `storage_backend == "gcs"`; otherwise short-circuit straight to
  `url = None` (the single-file route also skips the backfill-and-GCS-lookup
  entirely for non-GCS files, avoiding a wasted GCS call). **Response
  shapes for both routes stay byte-for-byte identical to today** — no new
  fields on either route. This was a deliberate correction made during
  design: the batch route's response is a flat `{mfid: url_or_null}` dict
  with no room for extra per-file fields without a breaking shape change,
  and this route has a documented history in this project of exactly that
  kind of regression being fixed before. Catalog fields live on the
  `AssociatedFileRead`-returning routes instead, cleanly separating "what is
  this file and where does it live" from "give me bytes."
- **`POST /files/{file_id}/ingest`** (deprecated ingestion-request route):
  add a guard — `storage_backend != "gcs"` → `400` with a clear message,
  instead of silently creating an `IngestionRequest` that would fail deep in
  the ingestion pipeline against a file that was never in GCS.

## Access control

No change to the permission model. Remote-file catalog entries are gated by
the exact same `require_read_access`/`require_write_access` checks as GCS
files today — visible to anyone who can already read the dataset, editable
by anyone with write access. No new visibility tier, no per-file override.

## Migration

One additive migration:
```sql
ALTER TABLE associatedfile ADD COLUMN storage_backend TEXT NOT NULL DEFAULT 'gcs';
ALTER TABLE associatedfile ADD COLUMN access_note TEXT;
```
Every existing row becomes `storage_backend='gcs'` automatically via the
column default — no backfill script needed, no behavior change for any
existing file.

## Error handling

- No special validation on `storage_path`/`access_note` content for remote
  files — same as today's GCS `storage_path`, it's an opaque string the API
  doesn't parse. Garbage in means a useless catalog entry, not a crash.
- A download-link request for a non-GCS file returns `url: null` — the same
  signal already used today for "GCS blob missing," not a new error path a
  client has to learn to handle.
- Ingesting a non-GCS file returns `400` with an explicit message.

## Testing

- Creating a file with `storage_backend="globus"` (or any non-`"gcs"`
  value) succeeds, and omitting the field defaults correctly to `"gcs"`.
- `GET /files/{id}/download_link` and `GET /datasets/{dsid}/download_links`
  return `null` for non-GCS files without attempting a GCS call.
- `GET /files/{id}` exposes `storage_backend`/`access_note` correctly.
- `POST /files/{id}/ingest` rejects non-GCS files with `400`.
- Regression coverage: existing GCS-file flows (create, update,
  download-link, ingest) are unaffected by any of the above.

## Future considerations (not in this change)

Logged in `docs/skipped_routes.md`:
- Generalizing `AssociatedFile.dataset_id` to a resource-generic
  `resource_id` (mfid), matching the pattern already used by
  `ResourceACL`, `ScientificMetadataBinary`, and `DeletionRequest`. Would
  let samples/instruments attach files with no new table/route family, at
  the cost of a real migration (retype a live FK + backfill) and a small,
  likely-unmeasurable indexing cost (26-char mfid string keys vs. 4-byte int
  keys) — the same tradeoff those three models already made. No current
  driving use case.
