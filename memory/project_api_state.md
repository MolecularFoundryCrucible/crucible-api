---
name: project-api-state
description: Current state of key API routes, recent features, and design decisions
metadata:
  type: project
---

## Auth & Permissions
- `require_admin(ag_list)` alias exists in `auth_utils.py`
- `GET /users/{orcid}` — admin or self only
- `GET /account/apikey` — self-service key; `GET /users/{orcid}/apikey` — admin only
- `GET /user_apikey` — browser cookie flow

## Upload & Storage (feat/direct-prod-upload, merged to main)
- Files upload directly to `mf-storage-prod/{dsid}/{filename}` — no staging bucket
- `storage_path` set immediately on upload completion
- `AssociatedFile.filename` = relative path; `storage_path` = full GCS URI
- Ingestor reads from `/mnt/gcs-prod/`, no longer moves files
- Lazy probe kept in `files.py` for pre-migration files with null `storage_path`
- Ingestion pods mount `mf-storage-prod` at `/mnt/gcs-prod` (k8s-conf updated)

## Hard Deletion (merged to main)
- `DELETE /resources/{id}` — admin only, requires approved request or `?force=true`
- Approval (`PATCH /deletion_requests/{id}?status=approved`) triggers hard delete automatically
- `DeletionAuditLog` table — permanent record, survives after resource rows gone
- `GET /deletion_audit` / `GET /deletion_audit/{id}` — admin only
- GCS blobs preserved on hard delete — background task handles cleanup
- Migrations: `create_deletionauditlog.sql`, `add_project_id_to_deletionauditlog.sql`

## GCS Record Versioning (merged to main)
- `mf-record-prod` bucket with versioning enabled
- `app/utils/gcs_record_utils.py` — `write_record()`, `write_metadata()`, serializers
- Written as BackgroundTasks on POST/PATCH for datasets, samples, instruments, metadata
- `_audit` block: `revision`, `operation`, `actor`, `timestamp`, `content_sha256`, `_schema_version`
- Skip-if-unchanged via hash comparison

## UTC Timestamps (merged to main)
- `get_tz_isoformat()` renamed to `get_utc_isoformat()` — stdlib only, no pytz
- Returns `datetime.now(timezone.utc).isoformat()` → `2026-06-03T17:23:45+00:00`

## Resources Route Fixes (merged to main)
- `GET /resources/metadata/search` moved above `GET /resources/{resource_id}` (path collision fix)
- `GET /resources/{id}` now includes `deletion_request` field
- `POST /resources/{id}/delete` rejects instruments/projects with 400
- `get_active_deletion_request()` utility in `sql_utils.py`

## Project/User Routes
- `POST/DELETE /projects/{proj_id}/users/{orcid}` accepts `?email=` param
- `GET /users`, `GET /users/{orcid}` — admin or self only
- `sql_add_user_to_access_group()` does NOT commit — callers own transactions

## Permission System (planned, not implemented)
- Design doc at `/home/roncofaber/permission_system_plan.md`
- Four roles: viewer, contributor, editor, admin
- Role on `UserAccessGroupLink`; `all` group for platform admins
- `require_permission(resource_id, permission)` unified check

## Ingestion (crucible-ingestion repo)
- `data_ingestion.py` reads metadata directly from `ig` attributes — no JSON intermediary, no rclone
- `consumer_ingestion_process.py` resolves `mf-storage-prod/` paths to `/mnt/gcs-prod/`
- GCS client used for file existence check (bypasses FUSE cache)
- `google-cloud-storage` added to `pyproject.toml`

## Pending / Known Issues
- `DELETE /datasets/{dsid}` 501 stub removed — use `POST /resources/{id}/delete`
- Pagination debt: several list endpoints return raw lists (noted in memory)
- Access group missing for users created outside API (workaround: INSERT INTO accessgroup)
