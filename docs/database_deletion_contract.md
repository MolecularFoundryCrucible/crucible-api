# Database deletion contract

Status: Current API v3 contract. Storage-object deletion remains deferred.

## Scope

Crucible currently deletes database records only. Dataset objects in GCS are intentionally left in place. Storage cleanup remains deferred and must not be represented as completed by the API.

Only datasets and samples support either deletion path. Projects and instruments must be rejected before a deletion request is approved, an audit record is created, or direct deletion is attempted.

## Approval workflow

The reviewed workflow uses:

```http
POST  /resources/{resource_mfid}/delete
PATCH /deletion_requests/{request_id}?status=approved
PATCH /deletion_requests/{request_id}?status=rejected
```

Contributor or above may create a request. A pending request hides the resource from normal reads. Admin, owner, or platform admin may review it.

Approval permanently removes the supported resource's database records. Audit creation and database deletion occur in one transaction. If any database deletion fails, the approval status and audit record roll back with it.

Rejection retains the resource and restores it to normal filtered reads.

## Direct forced deletion

The direct path is a separate owner-level escape hatch:

```http
DELETE /resources/{resource_mfid}?force=true
```

The `force=true` query parameter is required. An approved deletion request is neither required nor accepted as a substitute for the explicit flag.

Direct forced deletion requires either effective owner permission on the resource or platform-admin authority. Resource admins can approve the reviewed workflow but cannot bypass it.

If a resource has no effective owner, only a platform administrator can force-delete it.

## Permission mapping

`Permission.DELETE` remains mapped to admin and governs review of deletion requests. `Permission.HARD_DELETE` is mapped to owner and governs direct forced deletion. A separate permission name keeps the stronger bypass distinct from ordinary deletion administration.

## Database behavior

Related database rows are deleted with bounded set-based statements rather than loading and deleting every related row individually. The deletion transaction includes the durable audit record and removal of the resource, entity, ACL, active deletion requests, metadata, links, file records, ingestion requests, thumbnails, and active upload sessions as applicable.

GCS deletion is not invoked. The existing database audit records that Crucible removed its catalog records, not that stored bytes were destroyed.
