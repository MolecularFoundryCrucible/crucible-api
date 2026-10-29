# Stored objects left behind by file deletion

Status: documented 2026-10-20, corrected 2026-10-28 (dataset hard delete), not scheduled. To be handled as part of decoupling file storage from GCS ([S3-compatible backends](multipart_storage_backends.md)), so the cleanup goes through the storage adapter instead of more GCS-specific code.

## Problem

`DELETE /files/{file_id}` (`app/api/routes/files.py`, `delete_file`) deletes the file's ingestion requests and its `associatedfile` row but never deletes the stored object. The bytes stay in the upload bucket with no record pointing at them: storage leaks, and data a user deleted is still held.

Related paths:

- Replacing a file retires the previous object through `retire_if_eligible` in `app/services/multipart.py`. It deletes only when the object is in one of our own upload buckets, its path lies under the file's own dataset, no other file record references the same path, and the delete is pinned to the recorded object version (generation).
- Hard-deleting a dataset does not remove its objects either: `_delete_gcs_blobs_for_dataset` in `app/utils/deletion_utils.py` exists but nothing calls it.

## Intended fix

- After committing the row deletion, retire the object through the same eligibility rules as replacement. Remote records (Globus, NERSC, shared paths), objects in other buckets, and paths still referenced by another record are left alone.
- Database first, object second: a storage failure is logged and the delete still succeeds, so the worst case is today's leak, never a record pointing at missing bytes.
- Tests: an owned object is removed; shared paths, remote records, and foreign buckets are kept.

## Existing orphans

Objects from files deleted before the fix remain in production buckets. Finding them means listing objects under each dataset prefix and comparing against `associatedfile.storage_path`; deleting them needs explicit authorization and should be a reviewed, dry-run-first script.
