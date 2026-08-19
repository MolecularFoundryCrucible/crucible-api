# API Contract Changes — Permission System Redesign (Group A-D)

Reference for updating API clients against the `feat/permission-roles` branch,
deployed to staging. Verified directly against the live OpenAPI schema at
`https://crucible.lbl.gov/testapi-staging/openapi.json`, not just source.

## New routes

### `POST /resources/{mfid}/transfer_ownership`

`?confirm=true` to execute; omit to preview without writing anything.

```
Request:  { "new_owner": str }   # ORCID, username, email, or SA MFID
Response: { "resource_id": str, "previous_owner": UserPublicRead | null, "new_owner": UserPublicRead }

UserPublicRead = { unique_id: str, username: str | null, first_name: str, last_name: str }
```

Requires owner-level permission on the resource. For a project, demotes the
previous owner to admin within the shared group. For a dataset/sample/
instrument, removes the old owner's ACL entry outright and creates a new one
on the new owner's personal group.

### `POST /resources/{mfid}/project`

`?confirm=true` to execute; omit to preview.

```
Request:  { "project_id": str }
Response: { "resource_id": str, "previous_project_id": str | null, "new_project_id": str }
```

Datasets/samples only — 400 for projects/instruments. Requires admin on the
resource and contributor-or-above standing in the target project.

### `POST /instruments/{instrument_mfid}/service_accounts/{sa_unique_id}` and `DELETE` (same path)

```
No request body.
Response: [ AccessGroupMemberRead, ... ]   # full operator-group member list after the change

AccessGroupMemberRead = {
  unique_id: str, first_name: str, last_name: str,
  username: str | null, email: str | null,
  is_service_account: bool | null, role: str,
}
```

Requires admin on the instrument (in practice platform-admin only for now —
no group holds admin on an instrument otherwise). `DELETE` on a binding that
doesn't exist returns 404.

## Changed routes

### `POST /instruments`

`instrument_id` is now a **required** request field (did not exist before).
Validated (`^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$`), checked for uniqueness,
returns 409 on collision. The response (`InstrumentResponse`) now includes
`instrument_id` too, alongside the existing `unique_id` (MFID) and
`instrument_name` (free-text display).

### `PATCH /projects/{proj_id}`

Body now rejects `project_lead_orcid`, `project_lead_email`,
`project_lead_username` with **422** (`extra="forbid"`). Use
`POST /resources/{mfid}/transfer_ownership` for leadership changes instead.

`project_id` itself is now an accepted field — **renames the project**.
Validated the same way as at creation (format, reserved names, collision
against other projects/access groups/instrument names); 409 on collision,
422 on malformed input. Safe to do at any time: every FK that names a
project stores its MFID, never `project_id`, and every dataset/sample
response resolves `project_id` live rather than from a stored value, so a
rename never strands existing data under the old name. Accepted fields are
now `project_id`, `organization`, `status`, `title`.

### `GET/PUT/DELETE /resources/{mfid}/access/{kind}/{principal}`, `PUT/DELETE /resources/{mfid}/access/public`

**Breaking field rename.** `AccessGrantRead`/`AccessGrantWrite`'s `role`
field is now `effective_permission` — same values (`viewer`/`contributor`/
`editor`/`admin`/`owner`), new JSON key, in both the `PUT .../access/...`
request body and every response from these routes (`GET .../access`,
`PUT`/`DELETE .../access/{kind}/{principal}`, `PUT`/`DELETE .../access/public`).
A 422 on `PUT` now says `effective_permission must be one of [...]` instead
of `role must be one of [...]`.

The legacy, platform-admin-only `POST`/`GET /resources/{mfid}/access_groups`
pair (superseded by the above, but still live) is affected differently:
`POST`'s response returns the `ResourceACL` row directly, so its `role` key
is now `permission` instead — a separate rename from `effective_permission`
above, since this route was never updated to the newer response shape.

### `GET /projects/{proj_id}`

New optional `?include_members=true`. Adds a `members` field: a list of
`{unique_id, username, first_name, last_name, role}` (public-safe — no
email, unlike `GET /projects/{proj_id}/users`) for every project member.
**Members-only** — a non-member gets `"members": null` regardless of the
flag, same pattern as `?include_metadata=` and the lead's email today.

Also a bug fix with no schema change: membership detection for the lead's
email and `?include_metadata=` was comparing the caller's group name
against `project_id` instead of the project's MFID, so it was only ever
true for platform admins — a real member calling this route without
admin rights got the non-member (`UserPublicRead`, no email, null
metadata) response even for their own project. Fixed; real members now
correctly get the fuller response.

### `POST/PATCH /projects/{proj_id}/users/{orcid}`

Both now return **409** if `?role=owner` (`role=5`) is requested — a
project has exactly one owner, and appointing or changing one now only
happens through `POST /resources/{mfid}/transfer_ownership`. Previously an
existing owner could grant a second member `owner` via either route (or
change their own standing away from it), silently desyncing
`Project.project_lead_orcid` from who actually held owner standing.

### `PATCH /datasets/{dsid}`

Body now rejects `owner_orcid` and `project_id` with **422**. Use
`transfer_ownership` / `POST /resources/{mfid}/project` instead. Accepted
fields: `dataset_name`, `instrument_name`, `data_format`, `measurement`,
`data_type`, `session_name`, `timestamp`, `public`.

### `PATCH /samples/{sample_id}`

Same shape: `project_id` rejected with **422**. Accepted fields:
`sample_name`, `sample_type`, `description`, `timestamp`, `public`.

### `POST` / `PATCH` / `DELETE` `/projects/{proj_id}/users/{orcid}`

All three now return the **full member list** instead of a single-user
response:

```
Response: [ AccessGroupMemberRead, ... ]
```

`PATCH` requires a `role` query param (no default — was previously optional
in shape). `DELETE` gains a 409 if the caller is the project lead trying to
remove themselves ("Transfer leadership first").

### `GET /users/{orcid}/projects`

Response is now `PaginatedResponse[Union[ProjectWithLead, ProjectMinimalRead]]`,
decided server-side per caller:

- self or platform admin → `ProjectWithLead`:
  ```
  { unique_id, project_id, organization, project_lead_orcid, status, title,
    lead, scientific_metadata, creation_time, modification_time }
  ```
- caller with standing in any instrument group → `ProjectMinimalRead`:
  ```
  { project_id: str, title: str | null }
  ```
- anyone else → 403

### `POST` / `DELETE` `/users/{orcid}/access_groups/{group_name}`

Now returns **400** if `group_name` names a real project — redirects callers
to `POST`/`DELETE /projects/{proj_id}/users/{orcid}` instead.

### `POST /access_groups/{group_name}/join`

Now 404s if the target group name isn't a real project.

## `POST /datasets`, `POST /samples`: flexible `owner` field (unrelated to the permission redesign, noted here anyway since this is the one doc client services check)

New optional `owner` field, alongside the existing `owner_orcid`. Accepts an
ORCID, username, email, or service-account MFID — resolved server-side the
same way `POST /resources/{mfid}/transfer_ownership`'s `new_owner` already
does. `owner_orcid` is unchanged and still works exactly as before (strict
ORCID/MFID match, no resolution); `owner` is the flexible superset, not a
replacement — pick whichever your integration prefers. Providing both in
the same request is a **400**. All existing create-on-behalf authorization
(project standing, service-account bypass) applies identically regardless
of which field named the owner.

## `POST /projects`: flexible `project_lead` field (same pattern as above)

New optional `project_lead` field, alongside the existing three explicit
`project_lead_orcid`/`project_lead_email`/`project_lead_username`. Accepts an
ORCID, username, or email, resolved the same way as `owner` above. The three
explicit fields are unchanged; `project_lead` is the flexible superset, not
a replacement. Providing `project_lead` together with any of the three is a
**400**, as is providing none of the four.

## Unchanged (no client-side schema change, but new server-side validation)

### `POST /datasets` / `POST /samples`

Request schemas (`DatasetCreate` / `SampleCreate`) are unchanged —
`owner_orcid` and `project_id` are still accepted on create. What changed is
validation: the effective owner (named `owner_orcid`, or the caller if
omitted) now needs contributor-or-above standing in the named project. With
no project given and a different named owner, the request is rejected with
400 unless the caller is a service account (which only needs the named owner
to be a real registered user). Expect new 400/403/404 responses on payloads
that previously succeeded unvalidated — no request/response field changes.

## Behavior fixes (no schema change)

### Datasets nested under a sample or project response

`GET /resources/{sample_mfid}`, `GET /samples/{sample_id}`, `GET /samples`
(with `include_links=true&include_datasets=true`), and the parent/child
dataset lineage routes (`GET /datasets/{id}/parents`, `/children`) previously
**500'd** for any dataset that had a registered instrument, because
`instrument_id` in the response is the instrument's slug (a string) but was
being read from the wrong underlying column (an internal integer). Fixed;
no schema change, just no more crash.

### `project_id` in dataset/sample responses

Previously echoed whatever `project_id` text was stored at creation time.
Now resolved live from the project's current name on every read path
(`get`/`list`/`search` for both datasets and samples, plus the generic
`GET /resources/{mfid}`). Matters if you renamed a project (see
`PATCH /projects/{proj_id}` above) — old data now correctly shows the new
name instead of a name that no longer resolves to anything.

### `POST /access_groups/{group_name}/join`

Was silently broken for every real project (500, not surfaced clearly)
since the project-group MFID rename shipped — fixed. If any client had a
workaround in place for this, it's no longer needed.

## Verification method

Live schema pulled directly rather than relying on source reading alone:

```bash
curl -s https://crucible.lbl.gov/testapi-staging/openapi.json -o /tmp/staging_openapi.json
python3 -c "import json; d=json.load(open('/tmp/staging_openapi.json')); print(len(d['paths']), 'paths', len(d['components']['schemas']), 'schemas')"
```

Every schema and route above was cross-checked against `components.schemas`
and `paths` in that file, not just the source diff.
