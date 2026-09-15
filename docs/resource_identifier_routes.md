# Resource identifier routes

Status: API implementation complete. The dated staging identifier audit below is retained as a baseline. Production traffic-based compatibility retirement and downstream client migration are not confirmed by this repository.

This document is the API and Python client contract for stable identifiers and human-readable lookups.

## Identifier rules

- Projects, samples, datasets, instruments, and service accounts use an MFID as their stable identifier.
- People use an ORCID as their stable identifier.
- API responses expose either identifier as `unique_id`.
- Project slugs use `project_id`; instrument slugs use `instrument_id`; usernames identify users for human lookup.
- New or renamed project and instrument slugs must contain 3 to 25 characters. MFIDs contain exactly 26 characters.
- Sample and dataset names are not unique identifiers.

## Canonical GET routes

| Resource | Request |
|---|---|
| Project | `GET /projects/{project_mfid}` |
| Sample | `GET /samples/{sample_mfid}` |
| Dataset | `GET /datasets/{dataset_mfid}` |
| Instrument | `GET /instruments/{instrument_mfid}` |
| Person | `GET /users/{orcid}` |
| Service account | `GET /users/{service_account_mfid}` |
| Generic resource | `GET /resources/{resource_mfid}` |

Canonical responses include a non-null `unique_id`.

## Exact human lookup

Use exact collection filters when the caller has a human-readable identifier.

| Lookup | Request | Result count |
|---|---|---|
| Project slug | `GET /projects?project_id={slug}` | Zero or one |
| Instrument slug | `GET /instruments?instrument_id={slug}` | Zero or one |
| Username | `GET /users?username={username}` | Zero or one |
| Email | `GET /users?email={email}` | Zero or one |
| User stable ID | `GET /users?unique_id={orcid_or_mfid}` | Zero or one |

These requests return the normal paginated collection shape. The client must return the single item directly, raise its not-found error for zero items, and treat multiple items as an API integrity error. It must not make a second canonical GET because the item already includes `unique_id` and the full authorized representation.

Project lookup remains public-safe for authenticated callers. Project members and platform administrators retain member-only metadata and member-list access, while lead profiles remain public-safe. Exact user collection and singleton lookups return the same caller-authorized representation: self and platform administrators may receive email, while other authenticated callers receive `UserPublicRead`. Broad directory listings and batch resolution remain public-safe. `permissive=true` does not make `unique_id`, `username`, or `email` filters perform substring matching.

An exact instrument slug lookup searches every lifecycle state unless the caller explicitly supplies `status`. A normal instrument list still defaults to `status=active`.

Samples and datasets do not support name lookup through singleton routes. Use `/samples/search?q=` or `/datasets/search?q=` when searching by name.

## Temporary compatibility

New clients must use canonical paths or exact collection filters. These aliases remain temporarily available for existing integrations:

| Compatibility request | Resolution |
|---|---|
| `GET /projects/{project_id}` | Exact project slug |
| `GET /users/{username}` | Exact username |
| `GET /users/{email}` | Exact case-insensitive email |
| `GET /users/by-username/{username}` | Exact username |

Project updates, membership routes, and graph routes also accept either the project MFID or legacy project slug through `{project_ref}`. They resolve the project once and use its MFID for authorization and resource queries.

Compatibility singleton GET responses include `Content-Location` with the canonical path. Collection responses and nested routes do not include that header.

`GET /users/{user_id}` temporarily accepts username and email aliases. The deprecated `/users/{user_id}/datasets` and `/users/{user_id}/samples` compatibility routes accept the same references while clients migrate to `GET /datasets?accessible_to_user={user_ref}` and `GET /samples?accessible_to_user={user_ref}`. `GET /users/{user_id}/datasets/{dataset_mfid}` also accepts these references and returns the resolved canonical user ID. Other nested user routes, user mutations, API-key routes, and project membership targets remain canonical. Their `{user_id}` value must be an ORCID or service-account MFID.

## Python client dispatch

The client classifies the supplied reference locally and makes one request.

| Input | Request |
|---|---|
| Project MFID | `GET /projects/{mfid}` |
| Project slug | `GET /projects?project_id={slug}` |
| Instrument MFID | `GET /instruments/{mfid}` |
| Instrument slug | `GET /instruments?instrument_id={slug}` |
| Sample MFID | `GET /samples/{mfid}` |
| Dataset MFID | `GET /datasets/{mfid}` |
| Person ORCID | `GET /users/{orcid}` |
| Service-account MFID | `GET /users/{mfid}` |
| Username | `GET /users?username={username}` |
| Email | `GET /users?email={email}` |

Generated clients will see OpenAPI parameter names such as `project_ref`, `sample_mfid`, `dataset_mfid`, `instrument_mfid`, and `user_id`. The literal HTTP paths have not changed.

## Staging standing

The read-only staging audit found:

- No project or instrument slug that matches the 26-character MFID grammar.
- No project or instrument slug shorter than 3 characters.
- 48 project slugs longer than 25 characters.
- 2 instrument slugs longer than 25 characters.

Automatic client dispatch is unambiguous in staging because there are no MFID-shaped slug collisions. Existing out-of-range slugs remain readable through compatibility paths. Creates and explicit slug updates enforce the new 3-to-25-character range. Owners must choose replacement slugs before compatibility support is removed.

Run the same read-only audit in production before enabling automatic client dispatch there:

```sql
SELECT project_id FROM project WHERE project_id ~ '^[0-9a-hjkmnp-tv-z]{26}$';
SELECT instrument_id FROM instrument WHERE instrument_id ~ '^[0-9a-hjkmnp-tv-z]{26}$';
SELECT project_id FROM project WHERE length(project_id) NOT BETWEEN 3 AND 25 ORDER BY length(project_id) DESC;
SELECT instrument_id FROM instrument WHERE length(instrument_id) NOT BETWEEN 3 AND 25 ORDER BY length(instrument_id) DESC;
```

## Compatibility retirement checklist

1. Verify canonical MFID GETs, exact slug and user filters, redaction, and `Content-Location` headers against the deployed OpenAPI and build commit.
2. Confirm each supported client uses the dispatch table above without resolve-then-GET behavior.
3. Audit production identifiers before enabling automatic dispatch in a client that has not already migrated.
4. Measure use of project-slug and singleton user alias paths.
5. Arrange owner-selected replacements for out-of-range slugs.
6. Remove compatibility only in a separately announced breaking release after legacy traffic reaches the agreed threshold.

## Deferred work

- `ThumbnailRead` exposes the internal integer `dataset_id` and omits the canonical dataset MFID. A future contract should add `dataset_mfid` and decide whether to deprecate the internal ID.
- `AssociatedFileRead` calls the file identifier `mfid` instead of `unique_id` and exposes both `dataset_id` and `dataset_mfid`. A future compatibility plan should standardize those public names.
- Production slug audit and owner-selected renames are operational work, not automatic database migrations.

## Related documentation

- Route index: [`app/api/routes/readme.md`](../app/api/routes/readme.md)
- Permission migration API changes: [`docs/permission_system_api_changes.md`](permission_system_api_changes.md)
- Historical permission migration runbook: [`docs/archive/permission_migration_runbook_2026-08.md`](archive/permission_migration_runbook_2026-08.md)
