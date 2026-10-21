# Production API readiness assessment

Archived status: This pre-migration assessment was superseded by the completed August 2026 production rollout. Its blocking instructions and deployment statuses are historical.

Date: 2026-08-28

Reviewed commit: `e2f92d0`

Status: Do not start production database writes yet. The migration design is mostly sound, and the primary ACL queries and current backfills are set-based and efficient. The current API candidate still has production-blocking correctness and authorization issues, and the runbook is missing two deployable checkpoint commit IDs.

## Production blockers

### 1. Dataset routes can raise `NameError`

Status: Fixed locally. The module compiles and static analysis no longer reports `permission_filter` as undefined. Focused route tests timed out without output after 60 seconds due to the existing test-runner issue.

The imports in [`app/api/routes/datasets.py`](../../app/api/routes/datasets.py) previously omitted `permission_filter`, although the module uses it in numerous routes. This affected dataset search, lineage, thumbnails, keywords, dataset-sample relations, and graph endpoints.

This direct deployment blocker is fixed in the working tree.

### 2. Resource deletion requests are not authorization-checked

Status: Fixed and deployed to staging. Deletion requests now require contributor access. A partial unique index prevents more than one pending or approved request per resource, and concurrent index conflicts return `409`. Revision `d6e1f8a2c4b7` and its read-only preflight have been applied on staging.

Deletion request creation in [`app/api/routes/resources.py`](../../app/api/routes/resources.py) previously verified that the resource existed but did not require access to it. An authenticated caller who knew an MFID could request deletion of another user's resource. Pending requests were then omitted from normal listings.

This authorization and duplicate-request blocker is fixed in the working tree.

### 2a. Email-less project leads fail database insertion

Status: Fixed locally by revision `a9f3c2e7b614`; not yet applied to staging.

`POST /projects` correctly resolves an ORCID-less human by username and stores the canonical MFID in `project_lead_orcid`. The SQLModel field already permits `project_lead_email=None`, but the database retained a legacy `NOT NULL` constraint. PostgreSQL therefore rejected project creation before any access-group or ACL rows were committed. The revision makes the denormalized email column nullable and leaves `project_lead_orcid` as the canonical identity reference.

### 3. Graph routes can disclose inaccessible resources

Status: The two routes used by current clients are secured locally with six focused tests passing. `GET /entity_graph_cte/{resource_mfid}` now restricts traversal itself to readable, active resources, and `GET /project_graph/{project_ref}` filters both samples and datasets. Recursive traversal terminates safely if legacy lineage contains a cycle. The compatibility and cleanup plan is recorded in [`docs/graph_routes_migration.md`](../graph_routes_migration.md). Unused duplicate routes remain cleanup candidates after production access-log verification.

The client-used graph routes in [`app/api/routes/entities.py`](../../app/api/routes/entities.py) previously had inconsistent authorization:

- The recursive CTE authorized the root but could return inaccessible neighboring node identifiers and names.
- The project graph filtered datasets but not samples.
- Expanded graph traversal could reveal relationship topology through hidden intermediate resources.

Those client-used routes now enforce the documented filtering. The remaining duplicate graph implementations should be consolidated after the compatibility migration instead of retaining separate CTE, BFS, nested, and project-specific authorization logic.

### 4. Expanded links bypass target-resource permissions

Status: Fixed locally with four focused tests passing. Singleton and generic resource routes use a shared permission-aware link helper. Collection expansion remains batched and filters every target by READ access and active deletion state. The legacy `SampleResponse.datasets` field is preserved but now contains only readable, active datasets.

Linked-resource expansion in [`app/api/routes/resources.py`](../../app/api/routes/resources.py) previously filtered deleted targets but not inaccessible targets. Dataset and sample responses using `include_links` could expose names and MFIDs of resources the caller could not read.

Sample singleton responses also included complete linked dataset representations without independently checking dataset access. Every expanded target now receives its own READ filter.

### 5. Dataset instrument reassignment leaves stale access

Status: Fixed locally by freezing instrument reassignment through generic PATCH. Existing clients may resubmit the current `instrument_id` or `instrument_name`, but changing or clearing either value returns 409. Both PATCH fields are deprecated in OpenAPI.

The future dedicated atomic operation and its ACL requirements are recorded in [`docs/dataset_instrument_assignment.md`](../dataset_instrument_assignment.md).

### 6. Multipart upload exposes a bucket-wide credential

Status: Deferred because parallel upload is required for very large datasets. The agreed secure replacement retains the existing initiation and completion route paths while moving to XML multipart uploads and exact-object downscoped credentials. See [`docs/secure_parallel_upload_migration.md`](secure_parallel_upload_migration.md).

GCS multipart initiation in [`app/api/routes/gcs_storage.py`](../../app/api/routes/gcs_storage.py) returns a short-lived OAuth token with bucket-wide read/write scope. The supplied object prefix does not constrain that token.

Do not remove parallel uploads without a client replacement. During the future coordinated migration, preserve the existing route paths, replace parallel composite upload with XML multipart upload, and downscope the client token to the exact destination object. The existing exact-object resumable flow remains safer for clients that do not require parallel transfer.

### 7. Analysis jobs lack resource authorization

Status: Deferred. The future compatible hardening keeps all existing route paths, requires contributor permission, and returns a typed `202 Accepted` response. Durable idempotency and task tracking remain a later coordinated change. See [`docs/analysis_job_routes_migration.md`](analysis_job_routes_migration.md).

Dataset analysis actions in [`app/api/routes/datasets.py`](../../app/api/routes/datasets.py) can enqueue work without consistently verifying dataset existence or contributor access.

These should require contributor permission, return `202 Accepted`, and produce a typed task or request representation. Idempotency protection is also advisable.

### 8. User creation contains a definite list-handling bug

Status: Fixed locally. Proposal lookup occurs before the database transaction with bounded failure handling. Project IDs are flattened and deduplicated, project and group loading is batched, and user, identity-group, and membership creation is atomic. ORCID-less human creation generates an MFID and skips the proposal service. See [`docs/user_creation_reliability_migration.md`](user_creation_reliability_migration.md).

User creation in [`app/api/routes/users.py`](../../app/api/routes/users.py) appends the entire proposal list into `project_ids`, creating a nested list that is subsequently compared against `Project.project_id`.

The external proposal lookup also happens inside the database transaction and has weak timeout and status handling. Resolve proposals before opening the write transaction.

### 9. Hard deletion is not atomic and can report false success

Status: Fixed locally for database deletion. Approval now commits its audit and database deletion atomically. Direct deletion requires `force=true` and owner or platform-admin authority. Both paths reject unsupported resource types before mutation. Related rows use bounded set-based deletion. GCS cleanup is explicitly deferred. See [`docs/database_deletion_contract.md`](../database_deletion_contract.md).

Stored GCS objects remain after database deletion by current policy. Future storage cleanup will need its own reliable lifecycle because it cannot share the PostgreSQL transaction.

### 10. Sample creation has ownership and publication defects

Status: Fixed locally with focused schema and direct-route tests passing.

Dataset and sample creation now share one owner resolver. Omitted ownership resolves the authenticated identity, including service accounts, and both canonical owner columns are populated before ACL creation. Public sample ACLs use the persisted sample MFID.

Both create schemas reject unknown fields, reject `owner` together with `owner_orcid` at schema validation with 422, expose `owner` as the preferred reference, and mark `owner_orcid` deprecated. Internal `owner_user_id` is no longer accepted from clients.

### 11. Project resource identity is joined through the wrong field

Status: Fixed locally with a focused singleton response test.

The project resource relationship now joins `Resource.mfid` to `Project.unique_id`, matching project creation and the production identity backfill. Project singleton and collection loaders therefore resolve timestamps and metadata through the canonical project MFID rather than the mutable slug.

## SQL and performance

The important permission query design is good:

- Access checks use indexed `EXISTS` predicates rather than loading memberships into Python.
- ACL indexes support both resource-first and principal-first evaluation.
- Dataset and sample list expansion is generally batched.
- The principal backfills and group classification scripts now use a fixed number of set-based operations, so production size should mainly affect database execution time rather than network round trips.

Remaining concerns:

- Keyword filtering joins matching keywords directly. A dataset matching multiple keywords can appear more than once and corrupt pagination totals. Use a correlated `EXISTS`.
- Deletion lists, join-request lists, and ACL representation building perform per-row lookups. These should use joins or batch resolution.
- Project member pagination loads the full membership list and slices it in Python.
- Hard deletion performs repeated row-by-row selects and deletes. Use bulk deletes or database cascades.
- File download-link generation performs sequential GCS existence and prefix-list operations for every file.
- Cursor lists fetch exactly `limit` rows, requiring an extra empty request at the end. Fetch `limit + 1`.
- Several paginated routes lack deterministic ordering, particularly files and relationship lists.

Likely missing indexes include:

- `associatedfile(dataset_id)` and preferably a uniqueness rule for `(dataset_id, sha256_hash)`
- `thumbnail(dataset_id)`
- `datasetsamplelink(dataset_id)`
- `samplelink(parent_sample_id)`
- `datasetkeywordlink(dataset_id)`
- `useraccessgrouplink(accessgroup_id)`
- Active deletion request lookup on `(resource_id, status)`

PostgreSQL does not automatically index foreign-key referencing columns.

One migration exception remains: `20260807_delete_dangling_project_samples.py` uses repeated per-sample deletion work. It is not in the normal production recipe, but it should be rewritten or carefully bounded if production preflight indicates that it is required.

## File and ingestion routes

Several storage operations need tightening:

- Associated-file PATCH exposes server-managed storage path, backend, hash, and size fields. An editor could potentially point a record at another object.
- Upload completion trusts the client-provided hash without comparing it to an expected initiation value or verified object hash.
- Storage-path recovery lists a whole dataset prefix and suffix-matches filenames, which is slow and ambiguous.
- Concurrent file creation can bypass application-level hash deduplication without a database uniqueness constraint.
- Ingestion requests commit before RabbitMQ publication. A publish failure leaves committed state without a queued job, and retry can duplicate the request.
- Ingestion PATCH accepts the broad database model rather than a restricted update schema.

Use a transactional outbox for ingestion and deletion work rather than attempting to coordinate database commits and external services directly.

## API contract and UX

The canonical MFID singleton plus exact collection-filter convention is sound. The repeated typed `accessible_to` selectors with AND semantics are also a good design.

Recommended contract improvements:

- Return `Content-Location` for exact collection resolutions that produce one resource, including project slug, instrument slug, and username lookups.
- Make creation return `201`, asynchronous work return `202`, and empty successful deletion return `204`.
- Stop returning the entire unpaginated member list after project or instrument membership mutations. Return the affected membership for add/update and `204` for removal.
- Replace dynamically inspected query parameters with explicit typed FastAPI parameters. Currently, unknown filter typos can be silently ignored and result in unexpectedly broad listings.
- Apply `extra="forbid"` consistently to write schemas while retaining explicitly declared deprecated aliases during the compatibility period.
- Restrict email lookup and batch email resolution to service accounts and platform administrators. Public-safe username and ORCID lookup can remain generally available.
- Cap all batch resolution and first-thumbnail request sizes.
- Standardize OpenAPI parameter names such as `dataset_mfid`, `sample_mfid`, `project_ref`, and `user_ref`. This does not change URL shape but makes generated clients consistent.
- Avoid introducing more action-like route names before versioning. Existing snake-case paths can remain for compatibility.

The generated OpenAPI contains 147 operations, no duplicate operation IDs, and 13 deprecated operations. Many routes still emit an unspecified `{}` response schema, including action routes, graph routes, authentication operations, deletion operations, and download-link routes. These should receive explicit response models before clients treat the artifact as a stable contract.

## Other notable defects

- Thumbnail creation deduplicates globally by base64 content and can move an existing thumbnail from one dataset to another.
- Instruments are readable without ACL checks through instrument routes but require READ through generic resource routes. Decide whether instruments are an authenticated public directory and document that explicitly.
- Instrument filter code claims case-insensitive matching but lowercases only the input, not the SQL column.
- New instrument creation now resolves a registered owner, defaults to the caller, and creates the corresponding owner ACL. Existing instruments with descriptive legacy `owner` values are unchanged and may still require case-by-case ownership transfer or cleanup.
- Projects have no cleanup lifecycle suitable for staging tests or inactive work. A dedicated archive subresource is preferred over deletion or reuse of the domain `status` field, but it is deferred to avoid adding complexity to the permission migration. See [`docs/project_archiving.md`](../project_archiving.md).
- OAuth login does not properly generate and verify random `state` and `nonce` values.
- API-key retrieval uses GET and returns a secret, which risks browser and intermediary caching.
- Resource timestamps are strings rather than database timestamps. This is a later schema improvement, not something to add to the permission cutover.

## Migration standing

The staging database is in good structural standing based on the completed audits and Alembic upgrade. Accepted ownerless projects, absent human admins, and inert custom groups do not block the migration under the decisions already made.

The deployment evidence is not yet sufficient for production:

- [`docs/permission_migration_runbook.md`](permission_migration_runbook_2026-08.md) still has no tested commit IDs for the unified expand-compatible build and unified identity build.
- The runbook and client documentation describe an older staging deployment than the current local candidate.
- Current local HEAD is `e2f92d0`, while the documented staging base is `297144c`.
- The pending staging smoke-test checklist is not complete.
- The full test suite collected 956 tests but stalled during execution. Targeted route tests also stalled, so there is no clean automated test result for this candidate.
- Static analysis found the dataset `permission_filter` failure described above.

Before production, fix the blockers, produce the three deterministic deployment artifacts, deploy the final candidate to staging, run the complete authorization and upload/deletion smoke matrix, capture the exact live OpenAPI artifact and commit, then rerun every read-only database preflight against production immediately before the snapshot and first write.

## Review order

Work through the findings in this order:

1. Fix failures that prevent routes from running.
2. Fix authorization leaks and unsafe credentials.
3. Fix creation, ownership, and identity correctness.
4. Make deletion and external work transactionally reliable.
5. Fix query duplication, N+1 lookups, missing indexes, and unstable pagination.
6. Tighten response models, status codes, filtering contracts, and client UX.
7. Complete staging deployment evidence and populate the production checkpoint commits.
