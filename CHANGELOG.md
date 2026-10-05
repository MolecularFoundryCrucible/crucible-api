# Changelog

This changelog records changes that affect API users, operators, or downstream clients. Add entries under `Unreleased` in the same change that modifies the observable contract or operational behavior. Internal-only changes do not require an entry.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added permanent deletion of empty projects and instruments through both the reviewed request workflow (admin on the resource may request) and the direct forced path (owner or platform admin). A project or instrument with assigned datasets or samples is rejected with 409 and a structured `blocking` count detail, checked at request creation, at approval with the row locked inside the transaction, and at forced deletion.
- Added a preview-and-confirm flow to `DELETE /resources/{resource_mfid}?force=true` for projects and instruments: without `confirm=true` it returns a `DeletionPreviewRead` describing blockers and what would be removed (group memberships, service-account bindings, join requests, cross-resource grants) and deletes nothing. Datasets and samples keep immediate forced deletion.
- Added a `preview` field to project and instrument deletion-request responses describing what the deletion removes or why it is blocked.
- Added `can_delete` to resource capabilities, true for projects and instruments when the caller may request or force deletion and the resource is empty; false for datasets and samples.
- Project deletion now removes the project access group with its memberships, legacy UserProjectLink rows, join requests, and the group's grants on other resources; instrument deletion removes the operator group with its service-account bindings and grants. Service-account User rows survive.
- Project and instrument deletions now write a `DeletionAuditLog` row on both approval and forced paths and schedule a final GCS record write with the removed-grants summary.
- While a project or instrument has a pending or approved deletion request it is hidden from project and instrument list and search endpoints, and new assignments (datasets, samples, instrument binding, project reassignment, member additions) are rejected with 409.
- Added `deletion_request` fields to project and instrument singleton responses so clients can see an active deletion request, mirroring dataset and sample details.
- Added `include_members=true` to `GET /instruments/{instrument_mfid}` and the exact `GET /instruments?instrument_id=` lookup, returning the people and projects holding a role on the instrument record to callers with a role on it and to platform administrators.
- Added `affiliation=maintainer` to `GET /instruments` and `GET /instruments/search` for instruments where the caller holds editor or admin.
- Instrument audit records now include `instrument_id`, `location`, `description`, `instrument_type`, and `status`, and lifecycle status changes write an audit record.
- Documented temporary ORCID authentication disablement, both production gates, and how the disabled settings persist across deployments.
- Added the Hub authentication security review, dependency security assessment, and deferred API-key lifecycle follow-up to the documentation.
- Updated the staging Hub client and both ORCID credential pins to Secret Manager version 2.
- Added pinned Google Secret Manager references for Hub client and ORCID credentials, environment-specific deployment settings, and a disabled-by-default deployment activation flag.
- Redacted Hub authentication validation failures and unexpected-error logs, and added no-store headers to all Hub authentication responses.
- Cloud Build now deploys the matching Hub cleanup CronJob with the API image SHA, initially suspended, and preserves its activation state on later builds.
- Added suspended hourly Hub authentication cleanup CronJobs for staging and main, packaged the cleanup script in the API image, and limited cleanup error logs to exception types.
- Added concurrent expiry indexes for Hub login transactions, handoff codes, access tokens, and token families.
- Documented Hub logout revocation, rotated-token support, session isolation, and Hub session cleanup requirements in OpenAPI and the login guide.
- Added separate ORCID-backed Hub sign-in, one-use code exchange, short-lived Hub access tokens, and authenticated refresh and revoke routes. Existing API keys remain supported.
- Added authorization-filtered `GET /embeddings` listing with signed cursor pagination, compatible offsets, optional exact totals, and exact ID, resource, chunk-index, and model filters.
- Added a `resource_embedding` table with IVFFlat cosine indexing for storing 768-dimensional, model-labeled text chunks linked to canonical resource MFIDs, plus authenticated `POST /embeddings/search` nearest-neighbor search with resource access filtering.
- Added human-requester deletion history, deterministic cursor pagination, direction and project/resource filters, canonical project MFID snapshots, and public-safe requester and reviewer summaries to deletion audit reads while retaining administrator-wide history, offset pagination, and stable ID fields. Service accounts cannot read deletion audit history.
- Added case-insensitive `resource_name`, `project_id`, `requester_name`, and semantic `status` sorting to deletion-request collections with nulls-last keyset pagination.
- Added nullable public requester and reviewer identity summaries to deletion-request responses while retaining their stable ID fields.
- Added request-time direction and opaque continuation cursors to deletion-request collections while retaining offset pagination and newest-first defaults.
- Added canonical `project_mfid` filtering and response fields to deletion-request collections.
- Added `accessible`, `submitted`, and `reviewable` scopes to `GET /deletion_requests` and caller-specific `can_review` capabilities to deletion-request responses.
- Added optional `Crucible-Privilege-Mode: normal|elevated` selection to authenticated routes, applied-mode and request-ID response headers, account authorization metadata, and structured elevated-request audit logs.
- Added authorization-filtered per-node graph expansion availability for upstream lineage, downstream lineage, and sample-dataset associations.
- Added bounded, authorization-filtered `GET /resources/{resource_mfid}/graph` traversal with typed sample and dataset nodes, typed lineage and association edges, persisted relationship types, deterministic ordering, depth and direction controls, completeness metadata, and online reverse-link indexes.
- Added human-platform-administrator-only `PATCH /service_accounts/{unique_id}` for changing a service account's platform role to `none`, `contributor`, or `admin`.
- Added optional `is_service_account` filtering to `GET /users/search` for public-safe person and service-account discovery.
- Added account-level workflow capabilities to `GET /account/profile` and administrator-only `GET /service_accounts` collection and detail routes with platform roles and non-secret API-key status.
- Added authorization-scoped `GET /datasets/facets` and `GET /samples/facets` endpoints with exact stored-value buckets, explicit null buckets, resolved owner/project/instrument references, and opaque bucket pagination. Dataset and sample collections now expose matching field-specific null filters.
- Dataset and sample facet endpoints now return canonical bucket labels and support deterministic raw-value, label, or count ordering through `sort=value|label|count` and `direction=asc|desc`, with stable ties and null buckets last.
- Public-safe user representations now include an `is_service_account` discriminator so clients can distinguish service identities from people without inferring account type from identifier format.
- Dataset, sample, instrument, and project collections and search endpoints now support repeatable `affiliation=owner` filtering based on authoritative owner permissions.
- Dataset, sample, and instrument collections and search endpoints now accept `owner_id` as the preferred exact human-or-service-account filter while retaining their existing owner filters as deprecated aliases.
- Dataset, sample, instrument, and project collections now accept `include_total` to explicitly request or suppress exact authorization-filtered counts. Omitting it preserves each endpoint's existing count behavior.
- Dataset and sample collections now support deterministic `sort=created|updated|name` with `direction=asc|desc`, timezone-aware time filters, and signed opaque continuation cursors.
- Dataset and sample collections now accept `anchor_mfid` to start an inclusive page at a resource within the authorized, filtered, and ordered collection.
- Project and instrument collections now support opaque continuation cursors over their existing deterministic orders while preserving offset pagination and no-cursor behavior.
- Dataset and sample creation now accept either `project_id` or `project_mfid`, or a matching pair. Existing `project_id` requests remain supported with unchanged authorization and error behavior.
- Dataset creation now accepts either `instrument_id` or `instrument_mfid`, or a matching pair. Existing `instrument_id` and legacy `instrument_name` requests remain supported.
- Added `project_id` or `project_mfid` and optional `project_scope=assigned|shared|all` filters to dataset and sample search. Existing `project_id` searches remain assigned-only by default, and filtered results include `project_relation` metadata.
- Added explicit `project_id` or `project_mfid` and optional `project_scope=assigned|shared|all` filters to dataset and sample collections. Matching slug and MFID pairs are accepted, conflicting pairs return 422, and filtered responses include additive `project_relation` metadata.
- Added fixed `image/jpeg` MIME metadata to thumbnail responses and documented the bulk first-thumbnail response schema in OpenAPI.
- Added `PATCH /datasets/{dataset_mfid}/thumbnails/{thumbnail_id}` for authorized thumbnail renaming and image replacement while preserving thumbnail identity and ordering.
- Added thumbnail IDs to bulk first-thumbnail responses so clients can target modification or deletion routes.
- Added a concurrent database index supporting dataset-scoped thumbnail listing, selection, and deletion.
- Added `GET /instruments/{instrument_mfid}/service_accounts` for authorized administrators to inspect instrument bindings.
- Added a server warning when an unbound service account successfully creates a dataset for a registered instrument. Enforcement remains deferred.
- Added optional caller-specific `capabilities` to project and instrument responses, including the highest non-owner role the caller may grant. Capabilities are populated for singleton, exact-slug, create, update, and instrument-status responses, and remain null on general collection and search results.
- Added an explicit optional `instrument_mfid` filter to `GET /datasets` for stable instrument-scoped dataset listing.
- Added optional relationship filters: `sample_mfid` on `GET /datasets` and `dataset_mfid` on `GET /samples`. The existing nested relationship routes remain compatible but are deprecated.
- Added caller-specific capabilities to dataset and sample detail responses. Generic resource detail responses now populate capabilities consistently for datasets, samples, projects, and instruments.
- Added nullable `instrument` and `project` identity references to dataset response models. Existing flat `instrument_id`, `instrument_name`, and `project_id` fields remain available for compatibility.
- Added a nullable `project` identity reference to sample response models. The existing flat `project_id` field remains available for compatibility.
- Added `include_datasets=false` to singleton sample and generic resource reads so clients can omit the legacy embedded dataset collection. `include_links` and `GET /datasets?sample_mfid=...` are the preferred relationship interfaces.

### Changed

- Project slugs are now matched case-insensitively in `GET /projects?project_id=`, the `GET /projects/{project_id}` compatibility alias, project filters, and project references in other routes, matching instrument slugs. Creating or renaming a project to a slug that differs from another only by case returns 409. Migration `b7e2d9c4a1f3` adds case-insensitive unique indexes on `project.project_id` and `instrument.instrument_id`; `migrations/data/20261005_audit_case_insensitive_slugs.py` audits for conflicting slugs first.
- Dataset responses now resolve `instrument_name` from the assigned instrument through `instrument_mfid`, so instrument renames are reflected immediately. Datasets without a registered instrument keep their stored label. Generic dataset PATCH accepts either the stored label or the current name as the unchanged value.
- Deprecated the `instrument_name` lookup on `POST /datasets` and the `instrument_name` filter on `GET /datasets`. The filter now matches the displayed name. Use `instrument_id` or `instrument_mfid`.
- `project_id` no longer has to differ from instrument display names.
- Instrument creation no longer grants the instrument operator group `editor` on the instrument record. `migrations/data/20261005_remove_instrument_group_record_grants.py` removes the grant from existing instruments.
- Deprecated `POST /users/{user_id}/access_groups/{group_name}` now rejects human users for instrument groups with 409. Instrument groups hold service accounts only.
- Updated production readiness to Hub authentication schema revision `e92a6b1c730f`.

- `GET /ingestion_requests` now returns the authorization-filtered collection for any caller with viewer access to the parent datasets; non-administrators no longer need to supply a dataset or file filter.
- Deletion-request responses now require caller capabilities, statuses and review decisions use documented enums, and collection ordering uses descending request time and request ID.
- Authenticated responses now use private caching, and elevated responses use `Cache-Control: private, no-store`. Omitted privilege mode preserves existing platform-administrator behavior.
- Kubernetes readiness configuration now expects database revision `7a1c4e8d2f90` for the platform contributor role.
- Added `platform_role=contributor` for platform-wide read and create access without administrator authority. New service accounts receive this role by default and may use `accessible_to_user` for project, sample, and dataset collections. `is_service_account` remains an identity discriminator, and non-admin service accounts are capped at contributor even when an ACL or group standing would otherwise grant a higher role.
- Removed legacy group-based administrator and resource-permission fallbacks. Platform administration now requires `platform_role=admin`, and project and instrument authorization now requires canonical resource ACLs.
- Project membership routes now reject editor, admin, and owner roles for non-admin service accounts instead of storing roles that their authorization ceiling cannot use.
- API responses containing API keys now send `Cache-Control: no-store` on account, administrator, service-account creation, and key-rotation routes.
- Initial metadata creation, associated-file creation, upload initiation and completion, keyword association, ingestion-request creation, and same-project resource linking now require contributor access. Repeated associated-file creation returns the existing record without updating it.
- Associated-file creation now accepts `storage_path` for non-GCS catalog-only files so their location can be registered atomically.
- Dataset, sample, instrument, and project collection, search, and singleton queries now reject unknown parameters with 422. The generic resource singleton follows the same rule. Previously working exact dataset, sample, and instrument filters remain explicit and deprecated where appropriate.
- Dataset and sample pagination preserves the legacy behavior in which cursor takes precedence when cursor and offset are supplied together.
- Signed continuation cursors now use a compact internal Protobuf payload containing only page position. Request filters, ordering, resource type, and caller identity remain bound through the HMAC signature without being exposed in the token.
- API-owned resource creation and modification timestamps now use timezone-aware database columns, canonical UTC `Z` responses, database-generated values, immutable creation times, and automatically advanced modification times. Migration revision `8c4f2a1d7e60` converts approved legacy formats using the documented Pacific-time policy, converts empty and `NaN` sentinels to null, and rejects unsupported values. Revision `3f2b6a9c1d84` adds database enforcement. The producer-supplied dataset `timestamp` field is unchanged.
- Deployment readiness now reports degraded status when the connected database revision differs from `EXPECTED_DB_REVISIONS`.
- Trigram searches now normalize bounded input, treat prefix wildcard characters literally, bind the request-local similarity threshold, and use canonical unique IDs as deterministic relevance tie-breakers.
- Dataset, sample, instrument, and generic resource singleton GETs now resolve the public-safe owner object by default. Pass `include_owner=false` to omit it. Collection and search expansion remains opt-in.
- Large bulk first-thumbnail lookups now emit a structured operator warning above a configurable threshold without rejecting the request, and duplicate identifiers are removed before querying.
- Thumbnail uploads now release their database connection during image processing and recheck edit authorization before writing.
- Concurrent thumbnail uploads to the same dataset now serialize their dataset-scoped idempotency check, preventing identical requests from creating duplicate rows.
- Deletion-request and join-request collections now resolve project information in their page queries instead of issuing per-item queries. File detail responses reuse the already authorized dataset instead of lazily loading it again.
- Project membership now follows a strict hierarchy: editors manage contributor and viewer roles, admins manage editor and below, and owners manage admin and below. Project capabilities report the corresponding lower `max_grant_role`.
- Canonical and generic resource detail reads now share service loaders. `GET /resources/{resource_id}` mirrors canonical authorization semantics and avoids unnecessary deletion-request work for projects and instruments.
- Instrument display names now preserve caller-supplied casing instead of being forced to lowercase.
- Instrument collection filters and dataset assignment by `instrument_id` now resolve case-insensitively.
- Instrument search accepts an optional `status` filter with `active`, `maintenance`, and `decommissioned` values. Omitting it preserves the existing all-status search behavior.
- Project membership mutation routes now accept named roles (`viewer`, `contributor`, `editor`, `admin`, and `owner`) instead of integer enum values. `owner` remains rejected with 409 and requires the ownership-transfer route.
- Service accounts may no longer register instruments, including when they hold platform-admin status. Human platform administrators may continue registering an instrument for another identity.

### Fixed

- `POST /datasets` with an `instrument_name` shared by more than one instrument now returns 409 instead of a server error.
- Permanent dataset and sample deletion now removes associated resource embeddings instead of failing with a foreign-key violation.

- Escaped profile values in the legacy API-key page and removed dynamic values from its inline JavaScript.
- Hub code exchange now commits token issuance and code consumption together, and concurrent refresh reuse revokes the token family.
- Disabled raw Uvicorn access logs while retaining API request logs without query strings.
- Added a dry-run-by-default command for cleaning expired Hub authentication records.
- Hub sign-in now persists the token family before issuing related access and refresh tokens, avoiding a foreign-key error during code exchange.
- Deletion-request submitters can now read their own request detail without receiving deletion-review authority.
- Regenerated the checked-in OpenAPI artifact with dataset and sample facet operations and matching null filters.
- Identical thumbnail images uploaded to different datasets now remain independently owned instead of moving an existing thumbnail between datasets.
- Thumbnail uploads now verify dataset edit access before decoding or processing image data.
- Thumbnail deletion now applies dataset edit authorization in its initial lookup, consistently returning 404 for absent or inaccessible datasets and avoiding a redundant permission query.
- Thumbnail uploads now strictly validate base64 input and return stable errors without exposing image-library exception details.
- Thumbnail normalization now enforces configurable 50 MiB and 50 megapixel source limits, applies EXIF orientation, uses the first animation frame, composites transparency onto white, and accepts common raster formats only.
- Dataset thumbnail lists are now ordered by thumbnail ID, matching the bulk endpoint's definition of the first thumbnail.
- Join-request review now locks the pending request during its state transition, and the database prevents concurrent duplicate pending requests for the same user and access group.
- Project results from `GET /resources/metadata/search` now resolve titles through the canonical project MFID instead of the legacy project slug.
- Instrument updates now reject explicit null values for required identifiers, names, and locations with 422 instead of failing during persistence.
- Repeating a project add-member request with the member's current role remains idempotent, while requesting a different role now returns 409 and directs the caller to the role-update route instead of silently ignoring the requested change.
