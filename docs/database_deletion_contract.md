# Database deletion contract

Status: Current API v3 contract. Storage-object deletion remains deferred.

## Scope

Crucible currently deletes database records only. Dataset objects in GCS are intentionally left in place. Storage cleanup remains deferred and must not be represented as completed by the API.

Datasets, samples, projects, and instruments support both deletion paths. A project or instrument may only be deleted when it is empty: no datasets or samples assigned to it.

## Approval workflow

The reviewed workflow uses:

```http
POST  /resources/{resource_mfid}/delete
PATCH /deletion_requests/{request_id}?status=approved
PATCH /deletion_requests/{request_id}?status=rejected
```

Contributor or above may create a request for a dataset or sample. Admin on the resource may create a request for a project or instrument. A pending request hides the resource from normal reads. Admin, owner, or platform admin may review it.

Creating a request for a project or instrument that still has assigned datasets or samples returns 409. The response body carries a structured detail of the form `{"detail": "...", "blocking": {"datasets": 12, "samples": 3}}`.

Approval permanently removes the resource's database records. Audit creation and database deletion occur in one transaction. Before approving, the project or instrument row is locked and the emptiness check is repeated inside the transaction, so data assigned after the request was created still blocks approval with 409. Rejection stays allowed regardless of blockers and restores the resource to normal filtered reads.

Project and instrument deletion requests, including their review snapshots, carry a `preview` object describing what the deletion removes or why it is blocked.

## Direct forced deletion

The direct path is a separate owner-level escape hatch:

```http
DELETE /resources/{resource_mfid}?force=true[&confirm=true]
```

The `force=true` query parameter is required. An approved deletion request is neither required nor accepted as a substitute for the explicit flag.

Direct forced deletion requires either effective owner permission on the resource or platform-admin authority. Resource admins can approve the reviewed workflow but cannot bypass it.

If a resource has no effective owner, only a platform administrator can force-delete it.

Datasets and samples delete immediately. Projects and instruments are preview-first: without `confirm=true` the endpoint returns a preview and deletes nothing; with `confirm=true` it executes. Execution locks the project or instrument row and repeats the emptiness check, returning 409 with the blocking counts if datasets or samples were assigned after the preview.

The preview and executed response both use the `DeletionPreviewRead` model: `resource_id`, `resource_type`, a `deleted` flag, `blocking` counts, `memberships_removed` (project group members), `service_accounts_unbound` (instrument operator group members), `join_requests_removed`, and `removed_grants`, a list of `{resource_mfid, resource_type, permission}` for grants the deleted access group holds on resources other than the deleted resource itself.

## Pending state

While a project or instrument has a pending or approved deletion request:

- It is hidden from `GET /projects`, `GET /projects/search`, `GET /instruments`, and `GET /instruments/search`. The singleton lookups `GET /projects/{ref}` and `GET /instruments/{mfid}` remain reachable and carry a `deletion_request` field, mirroring dataset and sample detail reads.
- New assignments are rejected with 409: `POST /datasets` and `POST /samples` targeting the project or instrument, `PUT /datasets/{dataset_mfid}/instrument`, `POST /resources/{mfid}/project`, and `POST /projects/{project_ref}/users/{user_ref}`.

Rejecting the request removes the pending state and assignments are possible again.

## Permission mapping

`Permission.DELETE` is mapped to admin and governs review of deletion requests and requesting deletion of a project or instrument. `Permission.HARD_DELETE` is mapped to owner and governs direct forced deletion. A separate permission name keeps the stronger bypass distinct from ordinary deletion administration.

## Database behavior

Related database rows are deleted with bounded set-based statements rather than loading and deleting every related row individually. The deletion transaction includes the durable audit record and removal of the resource, entity, ACL, active deletion requests, metadata, links, file records, ingestion requests, thumbnails, and active upload sessions as applicable.

Deleting a project additionally removes its project access group, the group's grants on any resource, user memberships in the group (UserAccessGroupLink), legacy UserProjectLink rows, and all join requests for the group. Owners of other resources keep their own grants.

Deleting an instrument additionally removes its operator access group, the group's grants on any resource, and the service-account bindings (UserAccessGroupLink) in the group. The service-account User rows survive.

## Audit and slug reuse

Every project and instrument deletion writes a `DeletionAuditLog` row on both the approval and forced paths, with resource type, name (project slug or instrument name), requester, reviewer, times, and `project_id`/`project_mfid` filled with the project itself for projects and null for instruments. A final GCS record write with operation `delete`, including the removed-grants summary and counts, is scheduled best-effort.

After deletion the `project_id` or `instrument_id` slug is free to reuse. MFIDs are never reused, and the audit row keeps the deleted slug.

GCS deletion of stored bytes is not invoked. The existing database audit records that Crucible removed its catalog records, not that stored bytes were destroyed.
