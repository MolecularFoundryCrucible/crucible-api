# Deletion

This page describes how datasets, samples, projects and instruments are deleted, through a reviewed deletion request or by direct deletion, and how records pending deletion behave. Deletion is permanent and removes database records only.

## Routes

```http
POST   /resources/{mfid}/delete?reason=...
GET    /deletion_requests
GET    /deletion_requests/{request_id}
PATCH  /deletion_requests/{request_id}?status=approved|rejected[&reviewer_notes=...]
DELETE /resources/{mfid}?force=true[&confirm=true]
GET    /deletion_audit
GET    /deletion_audit/{audit_id}
```

Deletion applies to datasets, samples, projects and instruments; any other resource type returns 400.

## Permissions

| Action | Requirement |
|---|---|
| Request deletion of a dataset or sample | `contributor` on the record, or platform contributor or administrator |
| Request deletion of a project or instrument | `admin` on the record (`Permission.DELETE`), or platform administrator |
| Approve or reject a request | `admin` on the record (`Permission.DELETE`), or platform administrator |
| Delete directly | `owner` on the record (`Permission.HARD_DELETE`), or platform administrator |
| Read `GET /deletion_audit` with `scope=all` | Platform administrator |
| Read `GET /deletion_audit` with `scope=submitted` | Any human user, for their own requests |

Resource administrators can approve requests but cannot delete directly. A record without an owner can be deleted directly only by a platform administrator. Service accounts without platform-administrator authority cannot review requests, and no service account can read the deletion audit. `scope` defaults to `all`.

## Deletion requests

`POST /resources/{mfid}/delete` creates a `pending` request. A second request while one is `pending` or `approved` returns 409. A project or instrument must be empty: a project with assigned datasets or samples, or an instrument with assigned datasets, returns 409 with the counts:

```json
{"detail": {"detail": "Cannot delete project '...': it still has assigned datasets or samples.",
            "blocking": {"datasets": 12, "samples": 3}}}
```

Requests for projects and instruments, and their review responses, carry a `preview` object (see [Direct deletion](#direct-deletion)).

Approval deletes the record and writes the audit row in one transaction. For a project or instrument, the row is locked and the emptiness check repeated inside that transaction, so a dataset or sample assigned after the request still blocks approval with 409. Rejection is always allowed and returns the record to normal reads. Reviewing a rejected request, or approving an approved one, returns 409.
## Direct deletion

`DELETE /resources/{mfid}` requires `force=true`; an approved request does not replace it. Datasets and samples are deleted immediately and the response is `{"detail": "Resource ... permanently deleted"}`.

Projects and instruments are previewed first. Without `confirm=true` the route returns a preview and deletes nothing; with `confirm=true` it locks the row, repeats the emptiness check and deletes. A project or instrument that is not empty returns 409 with the blocking counts, with or without `confirm`.

The preview and the result of a confirmed deletion use `DeletionPreviewRead`: `resource_id`, `resource_mfid`, `resource_type`, `deleted`, `blocking`, `memberships_removed` (project or instrument members), `service_accounts_unbound` (instrument operator group members), `join_requests_removed`, and `removed_grants`, a list of `{resource_mfid, resource_type, permission}` for grants that the deleted access group holds on other records.

## Records pending deletion

A record with a `pending` or `approved` request is pending deletion.

For a dataset or sample:

- Exact lookups work: its detail (which carries `deletion_request`), its files, file detail and download link, and its ingestion requests and postprocessing runs by ID.
- Lists, searches, facets, filters and traversals exclude it: `GET /datasets`, `GET /samples`, `GET /files`, `GET /ingestion_requests`, `GET /postprocessing_runs`, the graph routes, and parent, child and linked-record routes. A filter such as `sample_mfid` or `dataset_mfid` naming it, or a traversal starting from it, returns 404.
- Writes return 409 `{"detail": {"code": "pending_deletion", "status": ..., "message": ...}}`: adding files or thumbnails, starting an upload, editing or deleting files, starting ingestion or postprocessing, and creating links. A multipart upload completed after the request returns 409 with `dataset_pending_deletion`. Status updates on existing ingestion requests and postprocessing runs are accepted so that running work can finish.

For a project or instrument:

- It is excluded from `GET /projects`, `GET /projects/search`, `GET /instruments` and `GET /instruments/search`. `GET /projects/{project_ref}` and `GET /instruments/{instrument_ref}` still return it, with `deletion_request`.
- New assignments return 409 `pending_deletion`: `POST /datasets` and `POST /samples` naming it, `PUT /datasets/{dataset_mfid}/instrument`, `POST /resources/{mfid}/project`, `POST /projects/{project_ref}/users/{user_ref}`, and `POST /users` with the project in its project list.

Creating or rejecting a request advances the record's `modification_time`.

## What is deleted

Every deletion removes the record's resource row, access grants, deletion requests, scientific metadata, embeddings and recent-resource entries, and in addition:

| Type | Also removed |
|---|---|
| Dataset | File records, ingestion requests, postprocessing runs, upload sessions, keyword links, thumbnails, sample links, parent and child links |
| Sample | Dataset links, parent and child links |
| Project | The project access group, its grants on any record, its memberships and its join requests |
| Instrument | The operator access group, its grants on any record and its service-account bindings; the service-account users remain |

Owners of other records keep their own grants.

## Stored objects

Deletion does not delete stored files on either storage backend: file objects remain in the bucket after their records are removed. A multipart upload completed after its dataset was deleted returns 409 `dataset_deleted`, and its assembled object is removed. The `python -m app.cli storage cleanup` command, run hourly when `STORAGE_CLEANUP_ENABLED=true`, removes only abandoned uploads and replaced file versions past their retention period.

When `RECORD_BUCKET` is set, deleting a project or instrument schedules a best-effort final `record.json` write with operation `delete`, the counts and the removed grants.

## Audit and identifier reuse

Every deletion, on both paths, writes a `DeletionAuditLog` row with the resource type and name, requester, reviewer, times, and the project's `project_id` and `project_mfid` (for a project, the project itself; null for an instrument). Direct deletions record the caller as requester and reviewer with the reason `direct forced deletion`.

After deletion the `project_id` or `instrument_id` slug can be reused; the audit row keeps the deleted slug. MFIDs are never reused.
