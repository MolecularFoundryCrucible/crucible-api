# Client migration summary

Status: deployed to production from `main` commit `24a24ab9ce74b01f841750df7003091ce4b57119` with database revision `3f2b6a9c1d84`. Verify `GET /health/ready` and the deployed OpenAPI before testing any environment. This repository does not establish downstream client rollout status.

Use this document to update Crucible clients for the identifier, permission, ownership, query, and response-contract changes. Production exposes [Swagger UI](https://crucible.lbl.gov/api/v3/docs) and [OpenAPI JSON](https://crucible.lbl.gov/api/v3/openapi.json). The repository pins the reviewed contract in [`openapi/crucible-api.json`](../../openapi/crucible-api.json).

## Contract authority and ownership

The generated `crucible-api` OpenAPI document is the authoritative HTTP contract. This guide explains migration intent and client actions, but it does not replace OpenAPI for paths, methods, fields, nullability, authentication, pagination, or deprecation metadata.

`GET /health/ready` reports the deployed API version, full Git commit, descriptive branch, and sorted Alembic schema revisions alongside database readiness. Deployment and smoke-test records should capture the commit and revisions rather than relying on an operator-supplied local checkout. The commit is authoritative for the deployed source; the branch is informational. `GET /health/live` remains a minimal process check, and `/metrics` remains the Prometheus scrape endpoint.

```json
{
  "status": "ok",
  "build": {
    "api_version": "3.0.0",
    "git_commit": "24a24ab9ce74b01f841750df7003091ce4b57119",
    "branch": "main"
  },
  "database": {
    "status": "ok",
    "latency_ms": 2.5,
    "schema_revisions": ["3f2b6a9c1d84"]
  }
}
```

Database or schema-revision lookup failure returns HTTP 503 with `status="degraded"`, `database.status="error"`, `database.latency_ms=null`, and an empty `database.schema_revisions` list. Build information remains present.

| Concern | Owner |
|---|---|
| HTTP schema, authorization, and error behavior | `crucible-api` |
| Python API, local reference dispatch, and CLI behavior | `nano-crucible` |
| Mobile models, repositories, caches, and presentation | `crucible-lens` |
| Graph presentation and graph-specific interaction | `crucible_graph_explorer` and other graph clients |
| Scientific extraction and metadata push behavior | `crucible-ingestion` |

Clients may remain handwritten, but their CI should compare declared routes and models against a deterministic OpenAPI artifact. Artifact retention, contract versioning, supported API-to-client version combinations, and the allowed client release lag are still ecosystem decisions. Until those policies exist, record the API commit and OpenAPI artifact used for each client migration.

## What every client must know

- Projects, samples, datasets, instruments, and service accounts use their MFID as the stable identifier. Resource responses expose it as `unique_id`.
- People use their ORCID as the stable identifier. Service accounts use an MFID. Both are returned as `unique_id`.
- Project and instrument slugs are human-readable lookup values, not stable identifiers. New or renamed slugs must contain 3 to 25 characters.
- Use a canonical singleton route when the input is an MFID. Use an exact collection filter when the input is a slug, username, or email. The collection result already contains the complete authorized representation, so do not resolve and then make a second GET.
- Normal ACL grants stop at `admin`. The `owner` permission is exclusive and can only be changed through the ownership-transfer route.
- User responses expose `platform_role` on private representations (`UserRead`, returned to the user themselves and to platform administrators) and `user_capabilities` on both private and public representations. `user_capabilities` is the reliable discriminator for what the caller may do to that user; do not infer capability from the presence of `email`. The field is named `user_capabilities`, not `capabilities`: any `capabilities` key on a user object decodes into a different required-field type in some clients. `AccountProfileRead` keeps its account-level `capabilities` object.
- Canonical dataset, sample, project, and instrument detail responses include optional caller-specific `capabilities` for UI guidance. Exact project and instrument slug lookups also populate it. General collection, search, lineage, and embedded-resource results leave it null. Dataset, sample, instrument, and generic resource singleton GETs resolve the public-safe `owner` object by default; pass `include_owner=false` to omit it.
- Relationship lists and graphs are permission-filtered. A client may receive 403 for an inaccessible starting resource or fewer related resources than before.
- The deprecated dataset `source_folder` field no longer exists in create or read schemas. Use associated-file records for file locations.

## Client impact matrix

Each client owner should confirm which listed routes it actually consumes. A repository named here is a review target, not evidence that every change applies to its current implementation.

| Client | Required changes |
|---|---|
| `nano-crucible` Python client and CLI | Add local MFID/reference dispatch, unwrap exact collection lookups, adopt the new access response, stop granting `owner`, replace deprecated user-resource and access-group calls, handle public-safe member-list mutation responses, and remove `source_folder`. |
| `crucible-lens` | Store and route with `unique_id`, use exact filters for human input, adopt any ACL and effective-access models it exposes, and handle redacted or permission-filtered responses as authorized views. |
| `crucible_graph_explorer` and `mcp-crucible` | Use canonical MFIDs for selected resources and accept that graph nodes, edges, and related-resource lists are filtered to the caller's readable resources. Validate any direct HTTP models against OpenAPI. |
| `crucible-upload-uis`, `crucible-tem-upload-ui`, and `sample-creators` | Review create payloads, remove `source_folder`, keep the returned `unique_id`, use flexible owner fields only where helpful, and handle new 400, 403, 404, and 409 validation failures. |
| `crucible-ingestion` and `ingestion-cloud-consumer` | Use canonical resource identifiers, require contributor standing to request ingestion, preserve associated-file backend information, and do not request ingestion for non-GCS files. The CZI ingestor must also migrate its direct email lookup from the old list shape and `orcid` field to the paginated public-safe result and `unique_id`. |
| `crucible-labs` | Update through its supported `nano-crucible` version and accept permission-filtered graph and relationship results. It should not reproduce server authorization logic. |
| Administrative tools | Use canonical ACL routes and enums, require `manage_acl`, use transfer ownership for owners, and use platform-admin credentials when inspecting another user's effective access. |

## Identifier dispatch

The client should classify a supplied reference locally and issue one request.

| Input | Request | Client result |
|---|---|---|
| Project MFID or slug | `GET /projects/{project_ref}` | Return the singleton response. Add `include_members=true` when the expanded authorized member list is needed. |
| Instrument MFID or slug | `GET /instruments/{instrument_ref}` | Return the singleton response. |
| Sample MFID | `GET /samples/{sample_mfid}` | Return the singleton response. |
| Dataset MFID | `GET /datasets/{dataset_mfid}` | Return the singleton response. |
| Person ORCID, service-account MFID, or username | `GET /users/{user_ref}` | Return the singleton response. |
| Email | `GET /users?email={email}` | Return the only item, report not found for zero, and report an integrity error for more than one. Another user's public-safe response does not echo the email. Email lookups may match more than one user until uniqueness is enforced; see [email uniqueness follow-up](email_uniqueness_followup.md). |

Singleton responses requested by slug or username include `Content-Location` pointing to the MFID or ORCID route. `GET /users/{email}` still resolves but email in paths is deprecated, as is `GET /users/by-username/{username}`. Exact collection filters (`GET /projects?project_id=`, `GET /instruments?instrument_id=`, `GET /users?username=`) remain available. See [resource identifier routes](../resource_identifier_routes.md) for the full convention.

Canonical resource detail responses, exact project and instrument slug-filter results, and supported create or update responses may include:

```json
{
  "capabilities": {
    "can_edit": true,
    "can_manage_access": true,
    "can_change_status": true,
    "can_transfer": false,
    "max_grant_role": "editor"
  }
}
```

`max_grant_role` is the highest non-owner role the caller may assign, or null when the caller cannot grant access. Clients should use capabilities to enable or hide actions but must still handle 403 and 409 responses because authorization is enforced server-side.

Project membership uses a strict hierarchy. Project editors receive `max_grant_role="contributor"`, project admins receive `max_grant_role="editor"`, and project owners receive `max_grant_role="admin"`. A caller cannot add, promote, demote, or otherwise modify a member at the caller's own project role.

For datasets and samples, `can_change_status` is always false because they do not expose the project or instrument lifecycle-status operation. General collection and search results leave `capabilities` null to avoid per-item ACL evaluation. Null means the server did not calculate capabilities for that representation, not that every action is denied.

## Ownership and project assignment

Preview an ownership change without writing:

```http
POST /resources/{resource_mfid}/transfer_ownership
Content-Type: application/json

{"new_owner": "{orcid|username|email|service_account_mfid}"}
```

Execute the same change with `?confirm=true`. Do not grant `permission=owner` through an ACL route or assign `role=owner` through project membership routes. Those requests now return 409.

Move a dataset or sample to another project by previewing and then confirming:

```http
POST /resources/{resource_mfid}/project
Content-Type: application/json

{"project_id": "{project_slug}"}
```

Use `?confirm=true` to execute. Dataset and sample PATCH requests no longer accept project reassignment fields. Dataset PATCH also rejects `owner_orcid`, and project PATCH rejects all project-lead identity fields.

Dataset instrument assignment is no longer mutable through generic PATCH. Omit `instrument_id` and `instrument_name` when updating a dataset. Existing clients may temporarily resubmit the current value, but changing or clearing it returns `409 Conflict`. Use `PUT /datasets/{dataset_mfid}/instrument` for assignment or reassignment. See [dataset instrument assignment](../dataset_instrument_assignment.md).

## Deletion contract

Deletion requests remain the normal reviewed workflow. Contributor or above may request deletion, and admin or above may approve or reject it. Approval permanently removes the dataset or sample database records.

Direct deletion is a separate bypass and now always requires `DELETE /resources/{mfid}?force=true`. It is allowed only for the effective resource owner or a platform administrator. Calls without the explicit flag, calls by resource admins, and calls targeting projects or instruments are rejected. Database deletion does not remove GCS objects.

## ACL contract

Use these routes for new integrations:

```http
GET    /resources/{mfid}/access
PUT    /resources/{mfid}/access/{kind}/{principal}
DELETE /resources/{mfid}/access/{kind}/{principal}
PUT    /resources/{mfid}/access/public
DELETE /resources/{mfid}/access/public
```

Listing or changing grants requires `manage_acl`. `AccessGrantRead` is now:

```json
{
  "principal_id": "01ABCDEF0123456789ABCDEFGH",
  "principal_type": "project",
  "permission": "admin",
  "slug": "my-project",
  "display_name": "My Project"
}
```

Client field changes:

| Old | New |
|---|---|
| `principal` | `principal_id` |
| `kind` | `principal_type` |
| `effective_permission` | `permission` |
| `group_name`, `group_id` | Removed because they expose internal access-group storage. |

Writable permissions are `viewer`, `contributor`, `editor`, and `admin`. Read responses may also contain `owner`. Principal types are `user`, `service_account`, `project`, `instrument`, `public`, `system`, and `unknown`.

The legacy `POST` and `GET /resources/{mfid}/access_groups` routes are deprecated. Nano's `AccessControlMixin.get_access_groups()` and `add_access_group()` must move to the canonical routes before compatibility support is removed.

## User and project membership calls

- `POST`, `PATCH`, and `DELETE /projects/{project_ref}/users/{user_ref}` now return the complete `AccessGroupMemberRead[]` member list. Member records include canonical identity, username, name, service-account status, and role, but never email.
- `PATCH` requires an explicit `role` query parameter.
- Project membership targets use `{user_ref}`: an ORCID, service-account MFID, or username. The `email` and `username` query overrides are deprecated.
- `GET /projects/{project_ref}?include_members=true` returns public-safe member records only to project members and platform administrators. Other callers receive `members: null`.
- `GET /projects?project_id={slug}&include_members=true` provides the same member expansion for one-request slug resolution. Using `include_members=true` without `project_id` returns 422.
- Generic `POST` and `DELETE /users/{user_ref}/access_groups/{group_name}` routes are deprecated. They reject identity and system groups, direct project membership to project routes, and reject adding human users to instrument groups.
- Instrument service-account membership should use `POST` or `DELETE /instruments/{instrument_ref}/service_accounts/{service_account_ref}`.
- Instrument maintainers are grants on the instrument record through `PUT` and `DELETE /resources/{instrument_mfid}/access/users/{user_ref}`. `GET /instruments/{instrument_ref}?include_members=true` and `GET /instruments?instrument_id={slug}&include_members=true` return them to callers holding a role on the instrument and to platform administrators. `affiliation=maintainer` lists instruments where the caller holds editor or admin. See [instrument access](../instrument_access.md).

## User resource access

Use typed selectors on the canonical paginated collections:

```http
GET /datasets?accessible_to_user={user_ref}
GET /datasets?accessible_to_project={project_ref}
GET /samples?accessible_to_user={user_ref}
GET /samples?accessible_to_project={project_ref}
GET /projects?accessible_to_user={user_ref}
GET /projects?accessible_to_project={project_ref}
```

`GET /users/{user_ref}/projects` lists only the projects a user is a member of, each with the user's standing `role` in that project's access group, while `GET /projects?accessible_to_user=` lists every project the user can read, including explicit grants and, for a selected platform contributor or administrator, all projects. Both require the same authority to inspect another user: `platform_role` `contributor` or `admin`. See [`GET /users/{user_ref}/projects`](../permission_system_api_changes.md#get-usersuser_idprojects).

References are resolved by the API, so users may be identified by MFID, ORCID, username, or email and projects by MFID or slug. Repeat parameters to request an intersection. All selectors use AND semantics, are deduplicated after resolution, and may total at most 10. They only narrow resources readable by the caller. With no selector, dataset and sample collections list resources accessible to the caller and the project collection retains its existing discovery behavior.

Inspecting another user requires platform-admin authority. Inspecting a project requires membership in that project or platform-admin authority. An unknown principal returns 404, an unauthorized inspection returns 403, and more than 10 selectors returns 422.

The unpaginated MFID-only routes `GET /users/{user_ref}/datasets` and `GET /users/{user_ref}/samples` remain as deprecated compatibility wrappers.

Relationship collections now use the canonical paginated collection routes:

```http
GET /datasets?sample_mfid={sample_mfid}
GET /samples?dataset_mfid={dataset_mfid}
```

The caller must be able to read both the referenced resource and each returned resource. These filters compose with the existing collection filters and pagination. `GET /samples/{sample_mfid}/datasets` and `GET /datasets/{dataset_mfid}/samples` retain their existing response contracts but are deprecated and return the standard `Deprecation` response header.

Singleton sample responses retain the legacy embedded `datasets` collection by default so existing clients do not break. The field is deprecated in OpenAPI. New clients should pass `include_datasets=false`, use `include_links=true` when lightweight relationship references are sufficient, and use `GET /datasets?sample_mfid={sample_mfid}` when complete paginated dataset responses are required. The generic `GET /resources/{resource_id}` route accepts the same `include_datasets` option when the resolved resource is a sample.

## Dataset and sample relationship references

Dataset response models now include nullable lightweight references alongside the existing flat compatibility fields:

```json
{
  "unique_id": "dataset-mfid",
  "instrument_id": "xrd-1",
  "instrument_name": "High Resolution XRD",
  "project_id": "project-slug",
  "instrument": {
    "unique_id": "instrument-mfid",
    "instrument_id": "xrd-1",
    "instrument_name": "High Resolution XRD"
  },
  "project": {
    "unique_id": "project-mfid",
    "project_id": "project-slug",
    "title": "Project title"
  }
}
```

`instrument` uses `InstrumentReferenceRead`, and `project` uses `ProjectReferenceRead`. The API returns current registered identity values when the canonical MFID resolves. An unresolved legacy relationship returns null while preserving the existing flat stored values. The fields are additive across dataset detail, collection, search, lineage, generic resource detail, and datasets embedded in sample responses. Existing clients do not need to change, and `instrument_id`, `instrument_name`, and `project_id` remain part of the API v3 response contract. `instrument_name` is display text resolved from the assigned instrument; datasets without a registered instrument return their stored label.

`POST /datasets` accepts either the registered `instrument_id` slug or canonical `instrument_mfid`. Clients may send both when they identify the same instrument; conflicting pairs return 422. An unknown `instrument_mfid` returns 404. Existing `instrument_id` requests remain supported. The `instrument_name` fallback is deprecated: it keeps its 400 response for an unregistered name and returns 409 when the name matches more than one instrument. The `instrument_name` filter on `GET /datasets` is also deprecated in favor of `instrument_mfid`.

`POST /datasets` and `POST /samples` accept either the project slug as `project_id` or the canonical `project_mfid`. Clients may send both when they identify the same project; conflicting pairs return 422. An unknown `project_mfid` returns 404. Existing `project_id` requests retain their previous authorization and error behavior. Successful responses continue returning the flat `project_id` and the structured `project` reference.

Dataset and sample name search accepts the same explicit project relationship filters as their collection routes: `project_id` or `project_mfid`, plus optional `project_scope=assigned|shared|all`. Omitting `project_scope` preserves the existing assigned-only behavior. A matching slug and MFID pair is accepted, a conflicting pair returns 422, and an unknown slug returns 404. When a project filter is active, each result reports `project_relation` as `assigned` or `shared`.

Sample response models now expose the same nullable `ProjectReferenceRead` as `project` while retaining the flat `project_id` field. It is populated across sample create, update, detail, collection, search, lineage, generic resource detail, and the deprecated dataset-to-samples relationship response. Clients may adopt the stable `project.unique_id` without changing their handling of existing fields.

## User email privacy

- Broad user collection, batch resolution, project lead, project membership, instrument operator, and access-group member responses never contain email.
- Exact `GET /users?unique_id=`, `?username=`, and `?email=` lookups return the same caller-authorized representation as the singleton route. Self and platform administrators may receive email; other callers receive the matching public-safe user with `unique_id`.
- Platform administrators receive the full directory with email and platform roles on broad `GET /users` listings. Non-administrators keep the group-scoped public-safe listing.
- `POST /users/resolve` continues to accept emails and returns public-safe user records keyed by canonical identifier.
- Email is returned only for the authenticated user's own profile, an exact self lookup, an exact platform-admin lookup, or an authorized user mutation response.
- Service-account status alone does not grant email disclosure. A service account may resolve an email without receiving it back.
- Email lookups may match more than one user until uniqueness is enforced; see [email uniqueness follow-up](email_uniqueness_followup.md).

## User collection queries and capabilities

`GET /users` supports the shared collection contract: `sort=name|username|unique_id|email` with `direction=asc|desc` (omitting `sort` keeps the last name, first name order; `email` is administrator only), `include_total`, signed opaque `cursor` pagination alongside `offset`, and exact filters `unique_id`, `username`, and `email` plus `first_name`, `last_name` (permissive substring or exact), `q` with optional repeatable `search_fields`, `is_service_account`, and administrator-only repeatable `platform_role`. Searching email through `q` or `search_fields` is administrator only. A supplied cursor takes precedence over `offset`, and changing filters, sort, direction, or caller while following a cursor returns 422.

Any undeclared query parameter is now ignored without filtering. The response carries a `Deprecation: @1799280000` (2027-01-07) header, and the parameters will be rejected with 422 at that announced date. `?projects=x` and `?id=1` no longer filter or fail.

`user_capabilities` is populated on singleton and exact lookups:

```json
{
  "can_view_private": true,
  "can_edit_profile": true,
  "can_manage_api_key": false,
  "can_change_platform_role": false
}
```

`can_view_private` and `can_edit_profile` are true for the user themselves and platform administrators. `can_manage_api_key` is true for platform administrators only. `can_change_platform_role` is true only for human platform administrators acting on a human who is not themselves. Use this object as the discriminator for action availability instead of checking for `email`. It is null on broad listings, `/users/search`, and `/users/resolve`. `platform_role` on `UserRead` is visible to the user themselves and to platform administrators.

Human platform administrators manage a user's platform role through `PATCH /users/{user_ref}/platform_role` with `platform_role` set to `none`, `contributor`, or `admin`. Service-account callers receive 403, callers cannot change their own role, service-account targets return 409 pointing at `PATCH /service_accounts/{service_account_ref}`, and demoting the last human platform administrator returns 409. Authority changes propagate through the in-process principal cache within the configured `AUTH_CACHE_TTL`, which defaults to 60 seconds.

Platform administrators can revoke every API key a user holds through `DELETE /users/{user_ref}/apikey`, which returns 204 and invalidates the revoked keys in the principal cache for up to the TTL. It works for human users and service accounts. The singleton and exact platform-admin user views also expose non-secret `api_key_status` (validity, creation date, expiration date) when a key exists.

`GET /users/{user_ref}/datasets/{dataset_mfid}` now returns:

```json
{
  "resource_mfid": "01ABCDEF0123456789ABCDEFGH",
  "user_id": "0000-0000-0000-0000",
  "effective_access": "editor"
}
```

Replace uses of the removed `read` and `write` Booleans. `effective_access` is one of `none`, `viewer`, `contributor`, `editor`, `admin`, `owner`, or `platform_admin`.

## Create and update payloads

- `POST /instruments` requires `instrument_id`, the 3-to-25-character instrument slug. Authenticated human callers may create an instrument for themselves, and human platform administrators may create one for another registered identity. Service accounts cannot register instruments. Omit `owner` to assign the caller, or supply a registered ORCID, service-account MFID, username, or email when acting as a human platform administrator. `owner_orcid` is the deprecated exact canonical alias. Arbitrary descriptive owner labels are no longer accepted.
- Instrument responses expose the canonical identity as `owner_orcid`. Singleton and generic resource reads populate the public-safe `owner` object by default; pass `include_owner=false` to omit it. Collection and search expansion remains opt-in with `include_owner=true`. Creation responses include the resolved owner object directly.
- `POST /datasets` and `POST /samples` use `owner` as the preferred owner reference. It accepts ORCID, service-account MFID, username, or email. The deprecated `owner_orcid` field remains an exact canonical identity lookup. Omitting both makes the authenticated identity the owner, including for service accounts. Sending both or sending unknown fields such as `owner_user_id` returns 422.
- Create responses include canonical `owner_orcid`. The optional `owner` response field is an expanded public-safe user object, not an echo of the request string.
- For datasets and samples, platform contributors and platform administrators may create for another registered identity. Ordinary human callers may create only for themselves. With `project_id` or `project_mfid`, the effective owner must be contributor-or-above in that project. Projectless create-on-behalf remains supported.
- Service accounts created through `POST /service_accounts` default to `platform_role=contributor`. This role grants platform-wide read and create access and permits one user reference through `accessible_to_user` when listing that person's projects, samples, or datasets. The reference may be an MFID, ORCID, username, or email. Use the same reference as `owner` when creating for that person.
- `GET /account/profile` includes a `capabilities` object for workflow presentation, including `can_manage_users` for platform administrators. Platform administrators can list and search service accounts through `GET /service_accounts`, then retrieve `platform_role` and non-secret key status through `GET /service_accounts/{service_account_ref}`. Project membership clients must not assign a role above contributor to a non-admin service account.
- Human platform administrators can change a service account's authority through `PATCH /service_accounts/{service_account_ref}` with `platform_role` set to `none`, `contributor`, or `admin`. Service-account callers cannot use this route, including service accounts that currently hold `platform_role=admin`. Authorization changes propagate through the in-process principal cache within the configured `AUTH_CACHE_TTL`, which defaults to 60 seconds.
- Resource-manager identity pickers can use `GET /users/search?q={query}&is_service_account=true` to search public-safe service-account profiles without requiring platform-administrator access. Omitting `is_service_account` preserves mixed user and service-account search.
- Initial scientific metadata for a newly created resource must use `POST /resources/{mfid}/metadata`. `PATCH` remains an editor operation for merging into an existing metadata record.
- Non-GCS associated files may include `storage_path` in `POST /datasets/{dataset_mfid}/files`. Clients should create the complete catalog-only record in one request instead of creating it and then patching the location.
- `crucible-upload-uis/prefect_backend.py` currently maps its `owner_orcid` argument to the removed `owner_user_id` request key. That client must send `owner` or `owner_orcid`; otherwise its intended researcher owner is discarded before the request and the authenticated service account becomes owner.
- `POST /projects` accepts either one existing explicit lead field or the flexible `project_lead` field. Sending both forms, or neither form, returns 400.
- `PATCH /projects/{project_ref}` accepts `project_id`, `organization`, `status`, and `title`. Updating `project_id` renames the project slug without changing its MFID.
- `PATCH /datasets/{dataset_mfid}` accepts dataset descriptive and publication fields but not owner or project reassignment.
- `PATCH /samples/{sample_mfid}` accepts sample descriptive and publication fields but not project reassignment.
- `PATCH /instruments/{instrument_ref}` no longer accepts `owner`. Use the ownership-transfer route.
- Platform administrators may create an ORCID-less human through `POST /users` by omitting `unique_id`; the API returns a generated MFID and sets `is_service_account=false`. Supplied `unique_id` or `orcid` values must be ORCIDs. Human user create and update payloads no longer accept `is_service_account`. Retrieve a test user's key through `POST /users/{mfid}/apikey`.
- Resource creation now fails atomically with 409 when a required owner, project, or public access group is missing. Clients should surface the error and must not assume that a partial resource was created.

The existing `POST /users` compatibility body remains wrapped:

```json
{
  "user_info": {
    "first_name": "Test",
    "last_name": "User One",
    "username": "test-user-one",
    "email": null
  },
  "project_ids": []
}
```

Use the returned `unique_id` MFID for canonical user routes. To authenticate as that user during staging tests, a platform administrator can retrieve its key once with `POST /users/{user_ref}/apikey`.

## Files and ingestion

- Remove `source_folder` from dataset create, update, read, cache, and serialization models.
- Use associated-file `storage_backend`, `storage_path`, and `access_note` fields for file locations.
- Requesting ingestion through `POST /files/{file_id}/ingest` requires contributor-or-above on the dataset. The deprecated dataset-scoped alias has the same rule.
- Non-GCS associated files return 400 when ingestion is requested.
- Deleting an associated file requires admin-or-above on the parent dataset. Updating its metadata still requires editor-or-above.

## Graphs, relationships, and public visibility

- Dataset, sample, project, and combined entity graphs omit resources the caller cannot read and omit edges attached to those resources.
- Relationship list routes require read access to the starting resource and filter their results by read permission and deletion state.
- `include_links=true`, `GET /resources/{mfid}/links`, and the legacy `SampleResponse.datasets` field omit linked targets the caller cannot read or that have an active deletion request. Clients must treat omitted links as an authorized view.
- Public visibility comes from the public viewer ACL. Clients must not infer authorization from a legacy `public` Boolean alone.
- Fewer nodes or related items are an authorized view, not evidence that records were deleted.

## Required client test cases

1. Fetch every resource type by MFID and confirm a non-null `unique_id`.
2. Resolve a project slug, instrument slug, username, and email with one collection request each.
3. Confirm exact lookup handling for zero, one, and unexpected multiple results.
4. Preview and confirm an ownership transfer, then verify that a generic owner grant returns 409.
5. Grant, list, and revoke a normal ACL using the new field names and enums.
6. Add, change, and remove a project member while accepting the full member-list response.
7. List datasets and samples for self with no selector and with `accessible_to_user=self`.
8. Verify repeated user and project selectors return their intersection and never broaden the caller's readable collection.
9. Verify another user's access is denied to an ordinary caller and allowed to a platform administrator.
10. Resolve a user by email and confirm that self/admin exact lookups may include email while other callers, broad collections, membership, operator, and project-lead responses omit it.
11. Create and read a dataset without `source_folder`.
12. Verify a viewer cannot request ingestion, a contributor can, and a non-GCS file is rejected.
13. Verify graph and relationship views do not expose inaccessible resources.

## Detailed references

- [Permission API changes](../permission_system_api_changes.md)
- [Identifier route contract](../resource_identifier_routes.md)
- [Remote file records](../remote_file_records.md)
- [Production OpenAPI](https://crucible.lbl.gov/api/v3/openapi.json)
- [API docs](https://crucible.lbl.gov/api/v3/docs)
