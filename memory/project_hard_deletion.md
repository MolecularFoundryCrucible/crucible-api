---
name: project-hard-deletion
description: Hard deletion feature — branch, PR, what was built, and next steps
metadata:
  type: project
---

Branch: `feat/hard-deletion` — PR #151 to `main`.

## What was built

- `app/utils/deletion_utils.py` — `hard_delete_resource()` dispatches to `_hard_delete_dataset()` or `_hard_delete_sample()` based on resource type. Cascades all related DB rows + GCS blobs.
- `DELETE /resources/{resource_id}` in `deletion.py` — admin only; requires approved deletion request by default, `?force=true` bypasses.
- Approval (`PATCH /deletion_requests/{id}?status=approved`) now triggers hard delete automatically via two-commit flow: first commit persists approval + audit log, second commit destroys resource rows.

## DeletionAuditLog

New ORM model in `core_models.py`, response schema `DeletionAuditLogRead` in `deletion.py`. Stores a permanent record of every hard deletion (resource_id has no FK since the row is gone). Migration: `migrations/create_deletionauditlog.sql`.

New routes (admin only):
- `GET /deletion_audit` — paginated, filterable by `resource_id`, `requester_id`, `reviewer_id`
- `GET /deletion_audit/{audit_id}` — single record

## Key fixes made during review (commit 63249fa)

1. Per-blob try/except in GCS loop (resilience)
2. Audit trail gap fixed via DeletionAuditLog + two-commit flow
3. Blob path extraction: `partition("/")[2]` instead of `removeprefix()`
4. `_DOWNLOAD_BUCKET` imported from `gcs_storage.py` instead of redefined
5. Double Resource SELECT eliminated — pass loaded object through

## Next item (not yet implemented)

Add `project_id` to `DeletionRequestRead`. Plan: extend `_to_read()` in `deletion.py` to do a secondary lookup on the entity (Dataset or Sample) and pull `project_id`. Add field to `DeletionRequestRead` schema. No model changes needed.
