# API Contract Changes - Permission System Redesign

Reference for updating API clients against the `feat/permission-roles` contract. The base release is live on staging, while the typed access selectors and stricter email redaction are in the next local deployment candidate. Start with `docs/client_migration_summary.md` for the changes grouped by client type. Use `docs/permission_migration_runbook.md` for the deployed revision and staging verification status. See `docs/resource_identifier_routes.md` for canonical identifiers, exact lookup filters, compatibility aliases, and current path parameter names.

## New routes

### `POST /resources/{mfid}/transfer_ownership`

`?confirm=true` to execute; omit to preview without writing anything.

```
Request:  { "new_owner": str }   # ORCID, username, email, or SA MFID
Response: { "resource_id": str, "previous_owner": UserPublicRead | null, "new_owner": UserPublicRead }

UserPublicRead = { unique_id: str, username: str | null, first_name: str, last_name: str }
```

Requires owner-level permission on the resource. For a project, demotes the previous owner to admin within the shared group. For a dataset/sample/instrument, removes the old owner's ACL entry outright and creates a new one on the new owner's personal group.

### `POST /resources/{mfid}/project`

`?confirm=true` to execute; omit to preview.

```
Request:  { "project_id": str }
Response: { "resource_id": str, "previous_project_id": str | null, "new_project_id": str }
```

Datasets/samples only. Returns 400 for projects/instruments. Requires admin on the resource and contributor-or-above standing in the target project.

### `POST /instruments/{instrument_mfid}/service_accounts/{sa_unique_id}` and `DELETE` (same path)

```
No request body.
Response: [ AccessGroupMemberRead, ... ]   # full operator-group member list after the change

AccessGroupMemberRead = {
  unique_id: str, first_name: str, last_name: str,
  username: str | null,
  is_service_account: bool | null, role: str,
}
```

Requires admin on the instrument. In practice this is platform-admin only for now because no group otherwise holds admin on an instrument. `DELETE` on a binding that doesn't exist returns 404.

## Changed routes

### `POST /instruments`

`instrument_id` is now a **required** request field (did not exist before). It must contain 3 to 25 characters, start with a letter or digit, and contain only letters, digits, underscores, or hyphens. It is checked for uniqueness and returns 409 on collision. The response (`InstrumentResponse`) includes `instrument_id` alongside the existing `unique_id` (MFID) and `instrument_name` (free-text display).

### `PATCH /projects/{project_ref}`

Body now rejects `project_lead_orcid`, `project_lead_email`, `project_lead_username` with **422** (`extra="forbid"`). Use `POST /resources/{mfid}/transfer_ownership` for leadership changes instead.

`project_id` itself is now an accepted field and **renames the project**. It is validated the same way as at creation (format, reserved names, collision against other projects/access groups/instrument names), with 409 on collision and 422 on malformed input. It is safe to change at any time: every FK that names a project stores its MFID, never `project_id`, and every dataset/sample response resolves `project_id` live rather than from a stored value, so a rename never strands existing data under the old name. Accepted fields are now `project_id`, `organization`, `status`, `title`.

### `GET /resources/{mfid}/access`, `PUT/DELETE /resources/{mfid}/access/{kind}/{principal}`, `PUT/DELETE /resources/{mfid}/access/public`

**Breaking access-grant contract.** `GET /resources/{mfid}/access` now requires `manage_acl`, so viewers, contributors, and editors receive 403 instead of seeing every principal with access. Resource owners, effective admins, and platform administrators can list the grants.

`AccessGrantRead` is now a public principal description rather than an access-group record:

```json
{
  "principal_id": "01ABCDEF0123456789ABCDEFGH",
  "principal_type": "project",
  "permission": "admin",
  "slug": "my-project",
  "display_name": "My Project"
}
```

Client changes:

- Rename `principal` to `principal_id`.
- Rename `kind` to `principal_type`.
- Rename `effective_permission` to `permission` in read responses and `PUT` request bodies.
- Remove `group_name` and `group_id`; they were internal database details.
- Read optional `slug` for a project slug, instrument slug, or username.
- Treat `principal_type` and `permission` as enums. Principal types are `user`, `service_account`, `project`, `instrument`, `public`, `system`, and `unknown`. Permissions are `viewer`, `contributor`, `editor`, `admin`, and `owner`.

`GET .../access`, `PUT .../access/{kind}/{principal}`, and `PUT .../access/public` return `AccessGrantRead`; the two DELETE routes return `{ "detail": str }`. Read responses may report `owner`, but normal grants accept only `viewer`, `contributor`, `editor`, or `admin`. A request to grant `owner`, or to modify an existing owner grant, returns **409** and directs the caller to `POST /resources/{mfid}/transfer_ownership`. An unknown permission value returns **422**.

The legacy, platform-admin-only `POST`/`GET /resources/{mfid}/access_groups` pair remains available with its older response models, but both operations are marked deprecated in OpenAPI. It is superseded by the routes above and should not be used by new clients. Nano Crucible's `AccessControlMixin.get_access_groups()` and `add_access_group()` still use these paths and must migrate before the compatibility routes are removed.

The legacy POST route maps its Boolean flags to the role model: `read=true&write=false` sets `viewer`, while `read=true&write=true` sets `editor`. It now updates an existing non-owner grant through the same authorization and grant checks as the canonical writer instead of silently returning the existing grant unchanged. `read=false` returns 422 and callers should use the canonical revoke route to remove access. The route cannot change an existing owner grant and returns 409 directing the caller to ownership transfer.

`DELETE /resources/{mfid}/access/{kind}/{principal}` now checks `manage_acl` before resolving the target principal or inspecting its grant. An unauthorized caller receives 403 for an existing resource regardless of whether the target is unknown, has no grant, has an ordinary grant, or owns the resource. Authorized ACL managers retain the existing 404, 409, and successful deletion behavior.

### `GET /projects/{project_ref}`

New optional `?include_members=true`. Adds a public-safe `members` list for every project member. A non-member gets `"members": null` regardless of the flag, following the same pattern as `?include_metadata=`. Project lead and member profiles never include email.

Also a bug fix: membership detection for `?include_metadata=` and `?include_members=` was comparing the caller's group name against `project_id` instead of the project's MFID, so it was only ever true for platform administrators. Real members now receive the authorized metadata and member list while the resolved lead remains public-safe.

The exact collection lookup `GET /projects?project_id={slug}&include_members=true` supports the same authorized member expansion so clients can resolve a slug without a second canonical request. `include_members=true` without an exact `project_id` filter returns 422.

### `POST/PATCH /projects/{project_ref}/users/{orcid}`

Both now return **409** if `?role=owner` (`role=5`) is requested. A project has exactly one owner, and appointing or changing one now only happens through `POST /resources/{mfid}/transfer_ownership`. Previously an existing owner could grant a second member `owner` via either route (or change their own standing away from it), silently desyncing `Project.project_lead_orcid` from who actually held owner standing.

### `PATCH /datasets/{dsid}`

Body now rejects `owner_orcid` and `project_id` with **422**. Use `transfer_ownership` / `POST /resources/{mfid}/project` instead. Accepted fields: `dataset_name`, `instrument_name`, `instrument_id`, `data_format`, `measurement`, `data_type`, `session_name`, `timestamp`, `public`.

### `PATCH /samples/{sample_id}`

Same shape: `project_id` rejected with **422**. Accepted fields: `sample_name`, `sample_type`, `description`, `timestamp`, `public`.

### `POST` / `PATCH` / `DELETE` `/projects/{project_ref}/users/{orcid}`

All three now return the **full member list** instead of a single-user response:

```
Response: [ AccessGroupMemberRead, ... ]
```

Every member record is public-safe and omits email. `PATCH` requires a `role` query param with no default; it was previously optional in shape. `DELETE` gains a 409 if the caller is the project lead trying to remove themselves ("Transfer leadership first").

### `GET /users/{user_id}/projects`

Response is now `PaginatedResponse[Union[ProjectWithLead, ProjectMinimalRead]]`, decided server-side per caller:

- self or platform admin → `ProjectWithLead`:
  ```
  { unique_id, project_id, organization, project_lead_orcid, status, title,
    lead, scientific_metadata, creation_time, modification_time }
  ```
- caller with standing in any instrument group → `ProjectMinimalRead`:
  ```
  { unique_id: str, project_id: str, title: str | null }
  ```
- anyone else → 403

### Resource lists for specified users and projects

`GET /datasets`, `GET /samples`, and `GET /projects` accept repeated `accessible_to_user` and `accessible_to_project` query parameters. User references may be MFIDs, ORCIDs, usernames, or emails. Project references may be MFIDs or slugs. The API resolves each reference once, deduplicates canonical principals, and returns resources accessible to every selected principal using AND semantics.

Selectors only narrow resources readable by the caller. Inspecting another user requires platform-admin authority. Inspecting a project requires membership in that project or platform-admin authority. At most 10 selectors may be supplied in one request.

```http
GET /datasets?accessible_to_user={user_ref}
GET /datasets?accessible_to_project={project_ref}
GET /samples?accessible_to_project={project_a}&accessible_to_project={project_b}
GET /projects?accessible_to_user={user_ref}&accessible_to_project={project_ref}
```

`GET /users/{user_id}/datasets` and `GET /users/{user_id}/samples` are deprecated compatibility routes. They now use the same effective-role and deletion filtering as the canonical collections but retain their existing unpaginated `List[str]` MFID responses for current Nano clients.

### User email disclosure

Broad `GET /users` listings, batch user resolution, project lead, project membership, instrument operator, and access-group member responses use public-safe user schemas and never return email. Exact `GET /users?unique_id=`, `?username=`, or `?email=` lookups return the same authorized representation as `GET /users/{user_id}`: self and platform administrators may receive email, while other authenticated callers receive `UserPublicRead`.

Email remains available through the authenticated caller's own profile, exact self lookups, exact platform-admin lookups, and authorized user mutation responses. Service-account status alone does not grant email disclosure. `POST /users/resolve` remains public-safe even when the request contains one identifier because it is a batch-resolution contract.

### `GET /users/{user_id}/datasets/{dataset_mfid}`

This route now reports the user's canonical effective access instead of the legacy `{read: bool, write: bool}` pair:

```json
{
  "resource_mfid": "01...",
  "user_id": "0000-...",
  "effective_access": "editor"
}
```

`effective_access` is one of `none`, `viewer`, `contributor`, `editor`, `admin`, `owner`, or `platform_admin`. The calculation applies group-standing caps, public access, and platform-admin bypass. The route accepts a user MFID, ORCID, username, or email, and remains limited to self or platform-admin inspection. A missing user or dataset returns 404.

This is an intentional breaking response change for Nano's `client.users.check_dataset_access()` and `crucible user check-access` command. Those clients must read `effective_access` instead of the removed `read` and `write` fields.

### `POST` / `DELETE` `/users/{user_id}/access_groups/{group_name}`

Both generic membership operations are now deprecated in OpenAPI. They remain platform-admin-only compatibility routes for existing instrument and custom groups.

The routes return **409** for identity groups and reserved system groups. A human or service account identity group is immutable membership containing only that identity at owner standing. They continue to return **400** for project groups and direct callers to `POST`/`DELETE /projects/{project_ref}/users/{orcid}`. POST now returns **404** for an unknown group instead of silently creating a custom group after a spelling mistake.

Nano Crucible's `client.users.add_to_access_group()` and `remove_from_access_group()`, including their service-account wrappers and CLI commands, use these deprecated paths. Instrument integrations should move to `POST`/`DELETE /instruments/{instrument_mfid}/service_accounts/{sa_unique_id}`. No instrument membership behavior changes in this API release.

### `POST /access_groups/{group_name}/join`

Now 404s if the target group name isn't a real project.

## `POST /datasets`, `POST /samples`: flexible `owner` field (unrelated to the permission redesign, noted here anyway since this is the one doc client services check)

New optional `owner` field, alongside the existing `owner_orcid`. It accepts an ORCID, username, email, or service-account MFID and resolves it server-side the same way `POST /resources/{mfid}/transfer_ownership` resolves `new_owner`. `owner_orcid` is unchanged and still works exactly as before (strict ORCID/MFID match, no resolution); `owner` is the flexible superset, not a replacement. Pick whichever your integration prefers. Providing both in the same request is a **400**. All existing create-on-behalf authorization (project standing, service-account bypass) applies identically regardless of which field named the owner.

The deprecated dataset `source_folder` field has been removed from `DatasetCreate`, `DatasetRead`, and dataset responses. File locations belong to associated-file records. Existing database columns and historical revision records are unchanged, while new dataset revision records omit the field.

## `POST /projects`: flexible `project_lead` field (same pattern as above)

New optional `project_lead` field, alongside the existing three explicit `project_lead_orcid`/`project_lead_email`/`project_lead_username`. Accepts an ORCID, username, or email, resolved the same way as `owner` above. The three explicit fields are unchanged; `project_lead` is the flexible superset, not a replacement. Providing `project_lead` together with any of the three is a **400**, as is providing none of the four.

## Unchanged (no client-side schema change, but new server-side validation)

### `POST /datasets` / `POST /samples`

`owner_orcid` and `project_id` remain accepted on create. A human caller may create only for themselves, and the effective owner must have contributor-or-above standing in the named project. Creating for another user returns 403 unless the caller is a service account or platform administrator. With no project given and a different named owner, the request is rejected with 400 unless the caller is a service account, which still needs the named owner to be a registered user. Service-account authorization for a target project is not enforced yet and remains deferred until instrument-to-project authorization is implemented. Expect new 400/403/404 responses on payloads that previously succeeded unvalidated.

## Behavior fixes (no schema change)

### Consistent platform-administrator checks

Routes that branch between platform administrators and ordinary callers now use the same transitional policy as administrator-only routes: either `platform_role="admin"` or membership in the legacy `admin` group is accepted. This fixes canonical platform administrators being denied full user-directory listing, unfiltered ingestion-request listing, project-member removal, and join-request listing or review when they were not also members of the legacy group. Legacy group compatibility remains temporary and can be removed centrally after migration.

### Atomic sample creation

`POST /samples` now commits the sample, shared resource row, public ACL, owner ACL, and project ACL in one transaction. If any database step fails, none of those rows remain and the request can be retried safely. The successful response contract is unchanged.

### Required access-group provisioning on creation

`POST /datasets` and `POST /samples` now return 409 and roll back creation when a specified owner has no personal access group or a specified project has no access group. Publishing also returns 409 when the virtual public group is not provisioned. The API does not recreate these groups automatically because an empty replacement group would not restore the required memberships and roles. Unpublishing remains a successful no-op when the public group is absent.

### Datasets nested under a sample or project response

`GET /resources/{sample_mfid}`, `GET /samples/{sample_mfid}`, `GET /samples` (with `include_links=true&include_datasets=true`), and the parent/child dataset lineage routes (`GET /datasets/{id}/parents`, `/children`) previously **500'd** for any dataset that had a registered instrument, because `instrument_id` in the response is the instrument's slug (a string) but was being read from the wrong underlying column (an internal integer). Fixed; no schema change, just no more crash.

### `project_id` in dataset/sample responses

Previously echoed whatever `project_id` text was stored at creation time. Now resolved live from the project's current name on every read path (`get`/`list`/`search` for both datasets and samples, plus the generic `GET /resources/{mfid}`). This matters after a project rename through `PATCH /projects/{project_ref}` because old data now shows the current name instead of a name that no longer resolves.

### `POST /datasets`, `POST /samples`: client-supplied `unique_id` cross-type collision

Previously this checked only the target's own table: a dataset's `unique_id` was checked against other datasets, never samples/projects/instruments. A client-supplied ID colliding with an *existing resource of a different type* slipped past that check and silently flipped the other resource's identity record to the new type in place. The shared identity table is now checked up front, so any same-type or cross-type collision returns a clean **409**, naming the other resource's type for a cross-type collision.

### `DELETE /files/{file_id}`

Now requires **admin** on the parent dataset (`Permission.DELETE`), not editor (`Permission.EDIT`). Deleting the dataset itself already requires admin; deleting one of its files was an inconsistently lower bar. `PATCH /files/{file_id}` (metadata update) is unchanged, still editor+.

### Ingestion requests and project dataset graphs

`GET /ingestion_requests`, `GET /ingestion_requests/{reqid}`, and `GET /projects/{project_ref}/dataset_graph` now use the same ACL permission evaluator as the corresponding dataset routes. Viewing remains available to viewer-or-above callers. `POST /files/{file_id}/ingest` and its deprecated dataset-scoped alias now require contributor-or-above on the parent dataset; viewers previously passed an unintended read-only check and now receive 403.

These read routes no longer authorize from the legacy `Dataset.public` Boolean. Public visibility requires a viewer grant to the public access group, as it does elsewhere. Platform administrators can read private ingestion requests and see private datasets in project graphs without belonging to their access groups.

### Scientific metadata search

`GET /resources/metadata/search` now uses the canonical read-permission filter exclusively. A stale `Resource.public=true` value without a public-group ACL no longer exposes scientific metadata through search. Valid public ACLs, private ACL access, and platform-admin bypass continue to work without any request or response change.

### Relationship lists and graphs

Sample parent, child, and dataset relationship routes now require read access to the starting resource and filter every returned resource through the canonical read-permission and deletion filters. Dataset-to-sample lists apply the same filtering. Sample, dataset, project, and combined entity graphs omit inaccessible nodes and any edges connected to them.

Response schemas are unchanged. Callers may receive 403 for an inaccessible starting resource or fewer related items and graph nodes where the previous implementation exposed relationships to inaccessible resources.

### Legacy authorization and shadow-mode removal

All active routes now use the role-based permission evaluator. The unused binary read/write helpers and permission shadow mode have been removed from the application and staging deployment configuration. This does not change an API contract or drop the legacy database columns, which remain available for rollback until the later contract migration.

### `POST /access_groups/{group_name}/join`

This was silently broken for every real project (500, not surfaced clearly) since the project-group MFID rename shipped. If any client had a workaround in place, it is no longer needed.

## Verification

Generate and inspect the deterministic local schema before deployment:

```bash
.venv/bin/python -m scripts.export_openapi
git diff --exit-code -- openapi/crucible-api.json
```

After deployment, compare it with `https://crucible.lbl.gov/testapi-staging/openapi.json` and run the smoke checks in `docs/resource_identifier_routes.md` and `docs/permission_migration_runbook.md`.
