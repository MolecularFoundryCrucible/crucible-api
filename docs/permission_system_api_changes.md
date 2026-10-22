# API Contract Changes - Permission System Redesign

Reference for the API v3 permission contract deployed to production. Verify `GET /health/ready` and the deployed OpenAPI before testing an environment. Start with `docs/client_migration_summary.md` for changes grouped by client type. See `docs/resource_identifier_routes.md` for canonical identifiers, exact lookup filters, compatibility aliases, and current path parameter names. The completed rollout is retained in `docs/archive/permission_migration_runbook_2026-08.md`.

## New routes

### `PATCH /users/{user_ref}/platform_role`

Human platform administrators only. Service-account callers receive 403 even when they hold `platform_role=admin`, and non-administrators receive 403.

```
Request:  { "platform_role": "none" | "contributor" | "admin" }   # extra=forbid; "support" is not accepted
Response: UserRead with user_capabilities
```

`{user_ref}` accepts an ORCID, service-account MFID, or username, like every `/users/{user_ref}` route. A caller cannot change their own role (403). A service-account target returns 409 pointing at `PATCH /service_accounts/{service_account_ref}`. Demoting the last human platform administrator returns 409; the check locks every human administrator row inside the transaction, so concurrent demotions cannot remove the last one. An unknown user returns 404. Authority changes propagate through the in-process principal cache within `AUTH_CACHE_TTL` (default 60 seconds).

### `DELETE /users/{user_ref}/apikey`

Platform administrators only. Revokes every `ApiToken` row the user holds, invalidating the revoked keys in this worker's principal cache; other workers clear them when the TTL expires. Returns 204. An unknown user returns 404. Works for human users and service accounts.

## Changed routes

### `GET /users`

Platform administrators receive the full directory on broad listings: `UserRead` items with email and `platform_role`, not scoped to shared groups. Non-administrators keep `UserPublicRead` items scoped to users who share an access group with them.

The collection accepts a strict query model: `sort=name|username|unique_id|email` with `direction=asc|desc` (case-insensitive; name sorts by last name then first name; email is administrator only; omitting `sort` keeps the last name, first name order), `unique_id` as tie-breaker, nulls last, `include_total` (omission preserves the legacy behavior that always returns a total), signed opaque `cursor` pagination alongside `offset` with cursor precedence, `next_cursor` on responses, exact `unique_id`/`username`/`email` filters, `first_name`/`last_name` (permissive substring or exact), `q` (case-insensitive substring over first name, last name, username, unique ID, and for administrators email) with optional repeatable `search_fields` to choose the searched fields, `is_service_account`, and repeatable administrator-only `platform_role` (non-admin callers receive 403).

Any other query parameter is ignored without filtering, and the response carries `Deprecation: @1799280000` (2027-01-07). The parameters will be rejected with 422 at that announced date. `?projects=x` and `?id=1` no longer filter or fail.

`user_capabilities` is populated on singleton lookups and every `GET /users` item, and remains null on `/users/search` and `/users/resolve`:

```
UserCapabilities = {
  can_view_private: bool,          # self or platform admin
  can_edit_profile: bool,          # self or platform admin
  can_manage_api_key: bool,        # platform admin only
  can_change_platform_role: bool,  # human platform admin, human target, not self
}
```

It is the reliable discriminator for what the caller may do to a user; do not infer capability from the presence of `email`. `UserRead` now also exposes `platform_role` to the user themselves and to platform administrators, and platform administrators see non-secret `api_key_status` on the singleton and on every `GET /users` item when a key exists.

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

### `POST /instruments/{instrument_ref}/service_accounts/{service_account_ref}` and `DELETE` (same path)

```
No request body.
Response: [ AccessGroupMemberRead, ... ]   # full operator-group member list after the change

AccessGroupMemberRead = {
  unique_id: str, first_name: str, last_name: str,
  username: str | null,
  is_service_account: bool | null, role: str,
}
```

Requires admin on the instrument: the instrument owner, admin maintainers, and platform administrators. Only service accounts can be bound. `DELETE` on a binding that doesn't exist returns 404. See [instrument access](instrument_access.md).

### `GET /instruments/{instrument_ref}/service_accounts`

Returns the current service accounts bound to the instrument's operator group using the same `AccessGroupMemberRead` response as the binding routes. Requires admin on the instrument.

## Changed routes

### `POST /instruments`

`instrument_id` is now a **required** request field (did not exist before). It must contain 3 to 25 characters, start with a letter or digit, and contain only letters, digits, underscores, or hyphens. It is checked for uniqueness and returns 409 on collision. The response (`InstrumentResponse`) includes `instrument_id` alongside the existing `unique_id` (MFID) and `instrument_name` (free-text display).

Authenticated human users may create an instrument for themselves. Human platform administrators may create one for another registered identity. Service accounts cannot register instruments, including when they hold platform-admin status. `owner` is optional and uses the same flexible identity resolution as dataset and sample creation. Omit it to make the authenticated caller the owner. The deprecated `owner_orcid` field remains an exact canonical identity lookup, and providing both fields returns 422.

Instrument display names preserve the casing supplied by the caller. Exact filters and dataset assignment by `instrument_id` remain case-insensitive. `PATCH /instruments/{instrument_ref}` rejects explicit null values for `instrument_id`, `instrument_name`, and `location` with 422; omit a field to leave it unchanged.

`GET /instruments/search` accepts an optional `status` query with `active`, `maintenance`, or `decommissioned`. Omitting the query preserves the existing behavior and searches across every status.

The response exposes the canonical identity as `owner_orcid`, matching dataset and sample responses despite the legacy field name. Instrument singleton and generic resource reads populate `owner` with a public-safe user object by default; pass `include_owner=false` to omit it. Collection and search expansion remains opt-in with `include_owner=true` and uses one batched user query. The API creates an owner ACL for that identity. The instrument operator group holds no grant on the instrument record. Maintainers are granted through the generic resource access routes, and `include_members=true` returns them to callers with a role on the instrument; see [instrument access](instrument_access.md). Arbitrary labels such as `"facility"` are no longer valid owner values unless they resolve to a registered username. Instrument PATCH no longer accepts `owner`; use `POST /resources/{instrument_mfid}/transfer_ownership`.

Dataset, sample, project, and instrument detail responses now share an optional caller-specific `capabilities` object:

```json
{
  "can_edit": true,
  "can_manage_access": true,
  "can_view_access": true,
  "can_change_status": true,
  "can_transfer": false,
  "max_grant_role": "editor"
}
```

`can_view_access` says whether `GET /resources/{mfid}/access` would be accepted (see below). `max_grant_role` is `viewer`, `contributor`, `editor`, `admin`, or null. Ownership is never grantable and continues to require the ownership-transfer route. Capabilities are populated by canonical singleton reads, generic `GET /resources/{resource_mfid}` detail reads, exact `instrument_id` and `project_id` collection lookups, supported create and update responses, and instrument-status responses. Dataset and sample detail responses set `can_change_status=false` because those resource types have no lifecycle-status operation. General collection, search, lineage, and embedded-resource responses leave the field null. Clients should use these values for presentation and preflight guidance while continuing to handle authorization failures from mutation routes.

### `PATCH /projects/{project_ref}`

Body now rejects `project_lead_orcid`, `project_lead_email`, `project_lead_username` with **422** (`extra="forbid"`). Use `POST /resources/{mfid}/transfer_ownership` for leadership changes instead.

`project_id` itself is now an accepted field and **renames the project**. It is validated the same way as at creation (format, reserved names, case-insensitive collision against other projects and access groups), with 409 on collision and 422 on malformed input. A rename may change only the casing of the project's own slug. It is safe to change at any time: every FK that names a project stores its MFID, never `project_id`, and every dataset/sample response resolves `project_id` live rather than from a stored value, so a rename never strands existing data under the old name. Accepted fields are now `project_id`, `organization`, `status`, `title`.

### `GET /resources/{mfid}/access`, `PUT/DELETE /resources/{mfid}/access/{kind}/{principal}`, `PUT/DELETE /resources/{mfid}/access/public`

**Breaking access-grant contract.** `GET /resources/{mfid}/access` is readable by any caller holding a role on the resource other than through public access, including a role reached through a project grant, and by platform administrators in elevated mode. Callers who can read the resource only because it is public, and platform contributors without a grant, receive 403. Each entry's `capabilities` are all false for callers who cannot manage access. Changing grants still requires `manage_acl`.

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

### `POST/PATCH /projects/{project_ref}/users/{user_ref}`

Both accept named role query values: `viewer`, `contributor`, `editor`, `admin`, and `owner`. Integer role values are no longer accepted. Both return **409** if `?role=owner` is requested. A project has exactly one owner, and appointing or changing one now only happens through `POST /resources/{mfid}/transfer_ownership`. Previously an existing owner could grant a second member `owner` via either route (or change their own standing away from it), silently desyncing `Project.project_lead_orcid` from who actually held owner standing.

Repeating `POST` for an existing member with the same requested role returns 200 without changing the membership. If the requested role differs from the member's current role, `POST` returns 409 and directs the caller to `PATCH`. Omitting `role` means `contributor` for this comparison.

Membership management follows a strict hierarchy. Editors may add or change contributors and viewers, admins may manage editors and below, and owners may manage admins and below. A caller cannot modify a member at the caller's own role. Project `capabilities.max_grant_role` reports `contributor` for editors, `editor` for admins, and `admin` for owners.

### `PATCH /datasets/{dataset_mfid}`

Body now rejects `owner_orcid` and `project_id` with **422**. Use `transfer_ownership` / `POST /resources/{mfid}/project` instead. Instrument reassignment is frozen until a dedicated atomic operation is available. The deprecated `instrument_name` and `instrument_id` fields accept only the current value for compatibility; changing or clearing either returns **409**. Other accepted fields are `dataset_name`, `data_format`, `measurement`, `data_type`, `session_name`, `timestamp`, and `public`.

### `PATCH /samples/{sample_mfid}`

Same shape: `project_id` rejected with **422**. Accepted fields: `sample_name`, `sample_type`, `description`, `timestamp`, `public`.

### `POST` / `PATCH` / `DELETE` `/projects/{project_ref}/users/{user_ref}`

All three now return the **full member list** instead of a single-user response:

```
Response: [ AccessGroupMemberRead, ... ]
```

Every member record is public-safe and omits email. `PATCH` requires a `role` query param with no default; it was previously optional in shape. `DELETE` gains a 409 if the caller is the project lead trying to remove themselves ("Transfer leadership first").

### `GET /users/{user_ref}/projects`

Response is `PaginatedResponse[ProjectWithLead]`:

```
{ unique_id, project_id, organization, project_lead_orcid, status, title,
  lead, scientific_metadata, creation_time, modification_time, role }
```

`role` is the target user's standing in that project's access group (`owner`, `admin`, `editor`, `contributor`, `viewer`), resolved in the page query. Projects with a pending or approved deletion request are hidden.

Available to the user themselves and to callers with `platform_role` `contributor` or `admin`, including service accounts. Anyone else receives 403. This is the same authority required to select another user with `accessible_to_user`. Instrument-group membership no longer grants access, and the former `ProjectMinimalRead` projection for instrument-group members has been removed.

The route lists projects the user is a **member** of, through standing in the project's access group. It differs from `GET /projects?accessible_to_user={user_ref}`, which lists projects the user can **read**: membership plus projects reachable through explicit resource grants, and every project when the selected user holds `platform_role` `contributor` or `admin`. Both routes are retained; choose by the question being asked.

| Question | Route |
|---|---|
| Which projects is this user a member of? | `GET /users/{user_ref}/projects` |
| Which projects can this user read? | `GET /projects?accessible_to_user={user_ref}` |

Both routes follow the shared collection contract in `docs/get_query_contract.md`: `sort` and `direction`, cursor and offset pagination, `include_total`, and `include_metadata`. `GET /users/{user_ref}/projects` also accepts a repeatable `role` filter on the target user's role.

### Resource lists for specified users and projects

`GET /datasets`, `GET /samples`, and `GET /projects` accept repeated `accessible_to_user` and `accessible_to_project` query parameters. User references may be MFIDs, ORCIDs, usernames, or emails. Project references may be MFIDs or slugs. The API resolves each reference once, deduplicates canonical principals, and returns resources accessible to every selected principal using AND semantics.

Selectors only narrow resources readable by the caller. Inspecting another user requires platform-contributor or platform-admin authority. Inspecting a project requires membership in that project, platform-contributor authority, or platform-admin authority. At most 10 selectors may be supplied in one request.

```http
GET /datasets?accessible_to_user={user_ref}
GET /datasets?accessible_to_project={project_ref}
GET /samples?accessible_to_project={project_a}&accessible_to_project={project_b}
GET /projects?accessible_to_user={user_ref}&accessible_to_project={project_ref}
```

`GET /users/{user_ref}/datasets` and `GET /users/{user_ref}/samples` are deprecated compatibility routes. They now use the same effective-role and deletion filtering as the canonical collections but retain their existing unpaginated `List[str]` MFID responses for current Nano clients.

### User email disclosure

Broad `GET /users` listings, batch user resolution, project lead, project membership, instrument operator, and access-group member responses use public-safe user schemas and never return email. Exact `GET /users?unique_id=`, `?username=`, or `?email=` lookups return the same authorized representation as `GET /users/{user_ref}`: self and platform administrators may receive email, while other authenticated callers receive `UserPublicRead`.

Email remains available through the authenticated caller's own profile, exact self lookups, exact platform-admin lookups, and authorized user mutation responses. Service-account status alone does not grant email disclosure. `POST /users/resolve` remains public-safe even when the request contains one identifier because it is a batch-resolution contract.

### `GET /users/{user_ref}/datasets/{dataset_mfid}`

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

### `POST` / `DELETE` `/users/{user_ref}/access_groups/{group_name}`

Both generic membership operations are now deprecated in OpenAPI. They remain platform-admin-only compatibility routes for existing instrument and custom groups.

The routes return **409** for identity groups and reserved system groups. A human or service account identity group is immutable membership containing only that identity at owner standing. They continue to return **400** for project groups and direct callers to `POST`/`DELETE /projects/{project_ref}/users/{user_ref}`. POST now returns **404** for an unknown group instead of silently creating a custom group after a spelling mistake.

Nano Crucible's `client.users.add_to_access_group()` and `remove_from_access_group()`, including their service-account wrappers and CLI commands, use these deprecated paths. Instrument integrations should move to `POST`/`DELETE /instruments/{instrument_ref}/service_accounts/{service_account_ref}`. No instrument membership behavior changes in this API release.

### `POST /access_groups/{group_name}/join`

Now 404s if the target group name isn't a real project.

## `POST /datasets`, `POST /samples`, `POST /instruments`: creation ownership contract

Use `owner` for new integrations. It accepts an ORCID, service-account MFID, username, or email and resolves it to the canonical user identity. The deprecated `owner_orcid` compatibility field remains an exact canonical identity lookup and may contain a human ORCID or service-account MFID despite its name. Omitting both assigns ownership to the authenticated identity, including service accounts. Providing both returns **422**.

Dataset and sample rows store both the canonical `owner_orcid` string and internal `owner_user_id`. Instrument rows store the canonical identity in their existing database `owner` column. All three create an owner ACL for that identity. `owner_user_id` is not a public request field and unknown create fields return **422**. Dataset, sample, and instrument responses expose the canonical identity as `owner_orcid`; their optional `owner` field is an expanded public-safe user object.

Instrument responses now follow the same read shape: `owner_orcid` contains the canonical identity and `owner` contains the optional public-safe expansion. Instrument creation returns the already-resolved owner object without another database query.

## `POST /users`: ORCID-less human identities

Platform administrators may omit `unique_id` when creating a human user. The API generates an MFID, skips proposal-service discovery, creates the user's identity group at owner standing, and stores `is_service_account=false`. When `unique_id` or its compatibility alias `orcid` is supplied, it must be an ORCID and proposal discovery retains its existing purpose.

`is_service_account` is no longer accepted by human user creation, self-profile update, or administrator user update schemas. Principal type is immutable through public user routes. Service accounts continue to be created only through `POST /service_accounts`.

The route now performs proposal lookup before opening a database transaction, uses a bounded timeout, returns 502 on upstream failure, deduplicates project IDs, fetches projects and their groups in bounded queries, rejects unknown explicitly requested projects, and creates identity and project memberships atomically. ORCID-less users make no external request.

An ordinary caller may create only for themselves, and for datasets and samples needs contributor-or-above standing in the supplied project; otherwise the API returns 403 naming the caller. Platform contributors and platform administrators, human or service account, may create datasets and samples in any project, owned by themselves or by any registered identity, without project membership. The owner receives an owner ACL and the project group receives admin, so the owner can read and manage the record without being a project member.

`platform_role=contributor` grants platform-wide read and create access. New service accounts receive this role by default and can read projects and resources, create resources and child records, and use `accessible_to_user` for another registered identity. `is_service_account` alone grants no global access. Non-admin service accounts remain capped at contributor even when an ACL or group standing would otherwise grant a higher role. They cannot edit existing values, delete resources, manage ACLs, transfer ownership, register instruments, or use platform-administrator routes.

`GET /account/profile` returns workflow capabilities for the authenticated principal. The flags describe whether the caller can manage service accounts and human users (`can_manage_users`), create projects, register instruments, create samples or datasets, and create for another identity. They are presentation guidance only; every operation remains independently authorized.

Authenticated routes accept optional `Crucible-Privilege-Mode: normal|elevated`. Omission preserves existing behavior. Explicit normal mode removes the platform-administrator bypass for that request while retaining the caller's ACL-derived permissions. Explicit elevated mode is limited to human platform administrators. Service accounts cannot explicitly elevate.

Authenticated responses return the applied `Crucible-Privilege-Mode` and an `X-Request-ID`. Normal responses use private caching unless a stricter directive already exists, while elevated responses use `Cache-Control: private, no-store`. Elevated use and rejected elevation attempts produce structured operational audit logs.

`GET /account/profile` now includes `authorization.platform_role`, `authorization.effective_privilege_mode`, and `authorization.can_elevate`. Its existing `capabilities` describe permissions effective in the applied request mode.

Platform administrators can use `GET /service_accounts` for a paginated, searchable directory and `GET /service_accounts/{service_account_ref}` for administrator-visible details. These responses expose `platform_role` and never expose the API key. The detail response includes non-secret key validity, creation date, and expiration date when a key exists. Project membership routes reject roles above contributor for a non-admin service account.

Human platform administrators can use `PATCH /service_accounts/{service_account_ref}` with `platform_role` set to `none`, `contributor`, or `admin`. Service accounts cannot call this route even when they currently hold `platform_role=admin`. The response returns the updated administrator-visible detail without an API key. Existing authenticated requests may retain the previous authority until the per-process principal cache expires; `AUTH_CACHE_TTL` defaults to 60 seconds.

Authorized resource managers can use `GET /users/search?q={query}&is_service_account=true` for public-safe service-account autocomplete. The optional filter is applied before ranking and limiting. It does not expose administrative service-account fields.

Project responses expose the acting principal's general `capabilities.max_grant_role`, while project member responses expose the stored membership role. A future target-aware membership contract may return effective role and assignable roles for a selected identity. Until then, clients should cap service-account choices at viewer and contributor and treat API authorization as authoritative.

Responses that contain an API key send `Cache-Control: no-store`. Creation and rotation remain the preferred service-account credential flows; administrative retrieval through `POST /users/{user_ref}/apikey` remains available for compatibility.

Revision `7a1c4e8d2f90` adds the PostgreSQL enum value and must run before deploying code that creates service accounts with the new default. It does not change existing users. Existing service accounts with `platform_role=admin` must be moved to `contributor` individually after their ingestion workflow is verified, then removed from the legacy `admin` group.

Contributor-level additive operations include initial metadata creation, associated-file creation, upload initiation and completion, keyword association, ingestion-request creation, and same-project dataset-to-sample and parent-child linking. Metadata overwrite, metadata merge, relationship-type changes, cross-project linking, resource updates, file updates, unlinking, deletion, ACL changes, and ownership transfer retain their existing higher permission requirements.

`POST /datasets/{dataset_mfid}/files` accepts `storage_path` when `storage_backend` is not `gcs`, allowing a complete catalog-only file record to be created without a follow-up editor operation. GCS storage paths remain assigned only by the upload completion route.

The deprecated dataset `source_folder` field has been removed from `DatasetCreate`, `DatasetRead`, and dataset responses. File locations belong to associated-file records. Existing database columns and historical revision records are unchanged, while new dataset revision records omit the field.

## `POST /projects`: flexible `project_lead` field (same pattern as above)

New optional `project_lead` field, alongside the existing three explicit `project_lead_orcid`/`project_lead_email`/`project_lead_username`. Accepts an ORCID, username, or email, resolved the same way as `owner` above. The three explicit fields are unchanged; `project_lead` is the flexible superset, not a replacement. Providing `project_lead` together with any of the three is a **400**, as is providing none of the four.

## Unchanged (no client-side schema change, but new server-side validation)

### `POST /datasets` / `POST /samples`

`project_id` and `project_mfid` remain optional on create. When either is supplied, the API validates the project and verifies that the effective owner has contributor-or-above standing. Platform contributors and administrators may create for another registered identity without a primary project.

## Behavior fixes (no schema change)

### Consistent platform-administrator checks

Routes that branch between platform administrators and ordinary callers use `platform_role="admin"` as the only source of platform-administrator authority. Membership in a group named `admin` does not grant platform authority. Project and instrument permissions require canonical resource ACLs and no longer fall back to group membership when ACL rows are missing.

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

`GET /ingestion_requests`, `GET /ingestion_requests/{request_id}`, and `GET /projects/{project_ref}/dataset_graph` now use the same ACL permission evaluator as the corresponding dataset routes. Viewing remains available to viewer-or-above callers. `POST /files/{file_id}/ingest` and its deprecated dataset-scoped alias now require contributor-or-above on the parent dataset; viewers previously passed an unintended read-only check and now receive 403.

These read routes no longer authorize from the legacy `Dataset.public` Boolean. Public visibility requires a viewer grant to the public access group, as it does elsewhere. Platform administrators can read private ingestion requests and see private datasets in project graphs without belonging to their access groups.

### Scientific metadata search

`GET /resources/metadata/search` now uses the canonical read-permission filter exclusively. A stale `Resource.public=true` value without a public-group ACL no longer exposes scientific metadata through search. Valid public ACLs, private ACL access, and platform-admin bypass continue to work without any request or response change.

### Relationship lists and graphs

Sample parent, child, and dataset relationship routes now require read access to the starting resource and filter every returned resource through the canonical read-permission and deletion filters. Dataset-to-sample lists apply the same filtering. Expanded `include_links` responses, `GET /resources/{mfid}/links`, and the legacy `SampleResponse.datasets` field independently filter every linked target. Sample, dataset, project, and combined entity graphs omit inaccessible nodes and any edges connected to them.

Use `GET /datasets?sample_mfid={sample_mfid}` and `GET /samples?dataset_mfid={dataset_mfid}` for typed, paginated sample-dataset relationship collections. The referenced resource and every returned resource are independently read-authorized. The legacy nested routes remain behaviorally unchanged, are marked deprecated in OpenAPI, and return `Deprecation: @1788307200`.

Dataset response models also include nullable `instrument` and `project` identity references. These contain only stable navigation and display fields, are populated without per-dataset queries, and do not grant access to the full related resource. Existing flat instrument and project fields remain unchanged for API v3 compatibility.

Existing response fields remain unchanged. Callers may receive 403 for an inaccessible starting resource or fewer related items and graph nodes where the previous implementation exposed relationships to inaccessible resources.

### Legacy authorization and shadow-mode removal

All active routes now use the role-based permission evaluator. The unused binary read/write helpers and permission shadow mode have been removed from the application and staging deployment configuration. This does not change an API contract or drop the legacy database columns, which remain available for rollback until the later contract migration.

### Database deletion

Approving a dataset or sample deletion request now commits the permanent audit and database deletion in one transaction. A database deletion failure rolls back both. Projects and instruments are rejected before audit or mutation.

Direct `DELETE /resources/{mfid}` now requires the explicit `?force=true` flag and effective owner permission or platform-admin authority. Resource admins retain the ability to approve deletion requests but cannot use the direct bypass. Database deletion currently leaves GCS objects in place.

### `POST /access_groups/{group_name}/join`

This was silently broken for every real project (500, not surfaced clearly) since the project-group MFID rename shipped. If any client had a workaround in place, it is no longer needed.

## Verification

Generate and inspect the deterministic repository schema after a contract change:

```bash
.venv/bin/python -m scripts.export_openapi
git diff --exit-code -- openapi/crucible-api.json
```

Compare the repository artifact with the target environment's deployed OpenAPI, confirm the commit and schema revisions through `GET /health/ready`, and run the applicable contract checks in `docs/resource_identifier_routes.md`.
