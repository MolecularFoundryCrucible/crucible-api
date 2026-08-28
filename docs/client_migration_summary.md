# Client migration summary

Status: the base permission and identifier release from API commit `297144c` is deployed to staging, and its live OpenAPI was verified against that commit. The typed access selectors, exact project member expansion, and stricter email-redaction contract described below are part of the next local deployment candidate and are not live yet. Its deterministic repository artifact is `openapi/crucible-api.json`. Client updates and production rollout are pending.

Use this document to update Crucible clients for the identifier, permission, ownership, and response-contract changes. The staging OpenAPI is available through [Swagger UI](https://crucible.lbl.gov/testapi-staging/docs) and [OpenAPI JSON](https://crucible.lbl.gov/testapi-staging/openapi.json).

## Contract authority and ownership

The generated `crucible-api` OpenAPI document is the authoritative HTTP contract. This guide explains migration intent and client actions, but it does not replace OpenAPI for paths, methods, fields, nullability, authentication, pagination, or deprecation metadata.

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
| Project MFID | `GET /projects/{mfid}` | Return the singleton response. |
| Project slug | `GET /projects?project_id={slug}` | Return the only item, report not found for zero, and report an integrity error for more than one. Add `include_members=true` when the expanded authorized member list is needed. |
| Instrument MFID | `GET /instruments/{mfid}` | Return the singleton response. |
| Instrument slug | `GET /instruments?instrument_id={slug}` | Apply the same exact-result handling. |
| Sample MFID | `GET /samples/{mfid}` | Return the singleton response. |
| Dataset MFID | `GET /datasets/{mfid}` | Return the singleton response. |
| Person ORCID | `GET /users/{orcid}` | Return the singleton response. |
| Service-account MFID | `GET /users/{mfid}` | Return the singleton response. |
| Username | `GET /users?username={username}` | Apply exact-result handling. |
| Email | `GET /users?email={email}` | Apply exact-result handling. Another user's public-safe response does not echo the email. |

The compatibility routes `GET /projects/{project_slug}`, `GET /users/{username}`, `GET /users/{email}`, and `GET /users/by-username/{username}` remain available temporarily. Compatible singleton responses include `Content-Location` pointing to the canonical route. New clients should not depend on these aliases.

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

- `POST`, `PATCH`, and `DELETE /projects/{project_ref}/users/{orcid}` now return the complete `AccessGroupMemberRead[]` member list. Member records include canonical identity, username, name, service-account status, and role, but never email.
- `PATCH` requires an explicit `role` query parameter.
- Project membership targets remain canonical ORCIDs or service-account MFIDs. Do not send a username in the `{orcid}` path position.
- `GET /projects/{project_ref}?include_members=true` returns public-safe member records only to project members and platform administrators. Other callers receive `members: null`.
- `GET /projects?project_id={slug}&include_members=true` provides the same member expansion for one-request slug resolution. Using `include_members=true` without `project_id` returns 422.
- Generic `POST` and `DELETE /users/{user_id}/access_groups/{group_name}` routes are deprecated. They reject identity and system groups and direct project membership to project routes.
- Instrument service-account membership should use `POST` or `DELETE /instruments/{instrument_mfid}/service_accounts/{service_account_mfid}`.

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

References are resolved by the API, so users may be identified by MFID, ORCID, username, or email and projects by MFID or slug. Repeat parameters to request an intersection. All selectors use AND semantics, are deduplicated after resolution, and may total at most 10. They only narrow resources readable by the caller. With no selector, dataset and sample collections list resources accessible to the caller and the project collection retains its existing discovery behavior.

Inspecting another user requires platform-admin authority. Inspecting a project requires membership in that project or platform-admin authority. An unknown principal returns 404, an unauthorized inspection returns 403, and more than 10 selectors returns 422.

The unpaginated MFID-only routes `GET /users/{user_id}/datasets` and `GET /users/{user_id}/samples` remain as deprecated compatibility wrappers.

## User email privacy

- Broad user collection, batch resolution, project lead, project membership, instrument operator, and access-group member responses never contain email.
- Exact `GET /users?unique_id=`, `?username=`, and `?email=` lookups return the same caller-authorized representation as the singleton route. Self and platform administrators may receive email; other callers receive the matching public-safe user with `unique_id`.
- `POST /users/resolve` continues to accept emails and returns public-safe user records keyed by canonical identifier.
- Email is returned only for the authenticated user's own profile, an exact self lookup, an exact platform-admin lookup, or an authorized user mutation response.
- Service-account status alone does not grant email disclosure. A service account may resolve an email without receiving it back.

`GET /users/{user_id}/datasets/{dataset_mfid}` now returns:

```json
{
  "resource_mfid": "01ABCDEF0123456789ABCDEFGH",
  "user_id": "0000-0000-0000-0000",
  "effective_access": "editor"
}
```

Replace uses of the removed `read` and `write` Booleans. `effective_access` is one of `none`, `viewer`, `contributor`, `editor`, `admin`, `owner`, or `platform_admin`.

## Create and update payloads

- `POST /instruments` requires `instrument_id`, the 3-to-25-character instrument slug.
- `POST /datasets` and `POST /samples` accept either the existing `owner_orcid` field or the flexible `owner` field. `owner` accepts ORCID, username, email, or service-account MFID. Sending both fields returns 400.
- `POST /projects` accepts either one existing explicit lead field or the flexible `project_lead` field. Sending both forms, or neither form, returns 400.
- `PATCH /projects/{project_ref}` accepts `project_id`, `organization`, `status`, and `title`. Updating `project_id` renames the project slug without changing its MFID.
- `PATCH /datasets/{dsid}` accepts dataset descriptive and publication fields but not owner or project reassignment.
- `PATCH /samples/{sample_id}` accepts sample descriptive and publication fields but not project reassignment.
- Resource creation now fails atomically with 409 when a required owner, project, or public access group is missing. Clients should surface the error and must not assume that a partial resource was created.

## Files and ingestion

- Remove `source_folder` from dataset create, update, read, cache, and serialization models.
- Use associated-file `storage_backend`, `storage_path`, and `access_note` fields for file locations.
- Requesting ingestion through `POST /files/{file_id}/ingest` requires contributor-or-above on the dataset. The deprecated dataset-scoped alias has the same rule.
- Non-GCS associated files return 400 when ingestion is requested.
- Deleting an associated file requires admin-or-above on the parent dataset. Updating its metadata still requires editor-or-above.

## Graphs, relationships, and public visibility

- Dataset, sample, project, and combined entity graphs omit resources the caller cannot read and omit edges attached to those resources.
- Relationship list routes require read access to the starting resource and filter their results by read permission and deletion state.
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

- [Permission API changes](permission_system_api_changes.md)
- [Identifier route contract](resource_identifier_routes.md)
- [Remote file records](remote_file_records.md)
- [Migration runbook](permission_migration_runbook.md)
- [Candidate OpenAPI artifact](../openapi/crucible-api.json)
- [Staging OpenAPI](https://crucible.lbl.gov/testapi-staging/openapi.json)
