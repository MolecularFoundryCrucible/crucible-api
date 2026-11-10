# Resource identifier routes

Status: identifier conventions apply to every route. The production identifier audit below must pass before deploying shape-based path resolution.

This document is the API and Python client contract for stable identifiers, human-readable handles, and how routes accept them.

## Identifier rules

- Projects, samples, datasets, instruments, and service accounts use an MFID as their stable identifier.
- People use an ORCID as their stable identifier.
- API responses expose the stable identifier as `unique_id`.
- Project slugs use `project_id`; instrument slugs use `instrument_id`; users also have a `username` and `email`.
- New or renamed project and instrument slugs must contain 3 to 25 characters. MFIDs contain exactly 26 characters. Usernames contain at most 24 lowercase characters. ORCIDs follow the `NNNN-NNNN-NNNN-NNNN` pattern, and emails contain `@`.
- Project and instrument slugs are unique and matched case-insensitively. Responses return the slug with its stored casing.
- Sample and dataset names are not identifiers. Samples and datasets are addressed only by MFID.

Because the forms never overlap, the API resolves a single value by its shape without ambiguity.

## Parameter naming

A parameter's suffix states what it accepts, so a client can tell from the name alone what to send.

| Suffix | Meaning | Matching | Examples |
|---|---|---|---|
| `x_id` | Canonical stable identity (ORCID, or MFID for a service account), or the slug for projects and instruments | Exact; slugs case-insensitive | `owner_id`, `user_id`, `project_id`, `instrument_id` |
| `x_mfid`, `{mfid}` | The MFID | Exact | `project_mfid`, `instrument_mfid`, `dataset_mfid` |
| `x_ref`, `{x_ref}` | Any identifier form for that entity | Resolved by shape | `member_ref`, `{user_ref}`, `{project_ref}` |
| Bare noun | An expanded object, in responses only | n/a | `owner`, `lead`, `members` |

| Entity | Paths | Query parameters and bodies |
|---|---|---|
| Project, instrument | `{x_ref}` | `x_id` and `x_mfid` as an agreeing pair (422 if they disagree) |
| User, service account | `{x_ref}` | `x_ref` for lookups that accept an ORCID, service-account MFID, username, or email; `x_id` only for exact stored-value filters such as `owner_id` |
| Dataset, sample | `{x_mfid}` | `x_mfid` |

The `_id` suffix never names an MFID in new fields. Legacy response fields that did so are deprecated and kept alongside their `_mfid` replacement.

New parameters follow these rules. Existing parameters that accept a user reference under a bare name (`owner` and `new_owner` in request bodies, `project_lead`, `accessible_to_user`) predate them and remain supported; renaming them to `_ref` forms with deprecated aliases is planned separately. The same applies to the `POST /users/resolve` body field `orcids`: it is matched against `unique_id`, so it accepts ORCIDs and service-account MFIDs despite the name, the response is keyed by `unique_id`, and clients keep sending `orcids` until the wire field is renamed with the deferred body-field renames.

## Path segments

| Segment | Accepts |
|---|---|
| `{project_ref}` | Project MFID or `project_id` slug |
| `{instrument_ref}` | Instrument MFID or `instrument_id` slug |
| `{user_ref}` | ORCID, service-account MFID, or username. Email still resolves for existing clients but is deprecated in paths because URLs are written to access logs. |
| `{service_account_ref}` | Service-account MFID or username. Human users are rejected with 404. |
| `{dataset_mfid}`, `{sample_mfid}`, `{mfid}`, `{entity_mfid}` | MFID only |

Examples:

```http
GET    /projects/{project_ref}
POST   /projects/{project_ref}/users/{user_ref}
GET    /instruments/{instrument_ref}
POST   /instruments/{instrument_ref}/service_accounts/{service_account_ref}
GET    /users/{user_ref}
GET    /service_accounts/{service_account_ref}
PUT    /resources/{mfid}/access/users/{user_ref}
PUT    /resources/{mfid}/access/projects/{project_ref}
GET    /project_graph/{project_ref}
GET    /datasets/{dataset_mfid}
```

Each segment is documented in OpenAPI with the forms it accepts. The route resolves the reference once and uses the stable identifier for authorization and queries. An unknown reference returns 404.

`GET /users/{user_ref}` and `GET /projects/{project_ref}` add a `Content-Location` header with the canonical path when the request used a non-canonical form.

## Bodies and query parameters

Projects and instruments use a pair of optional fields. Either may be supplied; when both are supplied they must identify the same resource, otherwise the request fails with 422. An unknown slug or MFID returns 404.

| Where | Fields |
|---|---|
| `POST /datasets`, `POST /samples` | `project_id`, `project_mfid` |
| `POST /datasets`, `PUT /datasets/{dataset_mfid}/instrument` | `instrument_id`, `instrument_mfid` |
| `POST /resources/{mfid}/project` | `project_id`, `project_mfid` |
| `GET /datasets`, `GET /samples`, facets, search | `project_id`, `project_mfid` |
| `GET /datasets`, `GET /datasets/facets` | `instrument_id`, `instrument_mfid` |

User references in bodies use one field that accepts an ORCID, service-account MFID, username, or email (`owner`, `new_owner`, `project_lead`). Exact owner filters use `owner_id`, which takes the stable identity. Access selectors (`accessible_to_user`, `accessible_to_project`) accept any form.

Responses that reference a project or instrument include both the slug and the MFID, for example `project_id` and `project_mfid`, or a nested reference with `unique_id` and the slug.

## Exact human lookup

Collection filters remain available when a client wants the normal paginated shape:

| Lookup | Request | Result count |
|---|---|---|
| Project slug | `GET /projects?project_id={slug}` | Zero or one |
| Instrument slug | `GET /instruments?instrument_id={slug}` | Zero or one |
| Username | `GET /users?username={username}` | Zero or one |
| Email | `GET /users?email={email}` | Zero or one |
| User stable ID | `GET /users?unique_id={orcid_or_mfid}` | Zero or one |

Exact user collection and singleton lookups return the same caller-authorized representation: self and platform administrators may receive email, while other authenticated callers receive `UserPublicRead`. `permissive=true` does not make `unique_id`, `username`, or `email` filters perform substring matching. An exact instrument slug lookup searches every lifecycle state unless the caller explicitly supplies `status`.

`GET /users/by-username/{username}` was removed in 4.0.0 and answers 410; use `GET /users/{user_ref}`.

## Client guidance

Clients should expose explicit keyword arguments (`project_id=`, `project_mfid=`, `instrument_id=`, `instrument_mfid=`) and build the request themselves. Because path segments accept either form, a client may put whichever identifier it has into a `{x_ref}` segment without a resolve-then-request round trip. Store MFIDs for durable references.

## Production identifier audit

Shape-based resolution relies on no slug or username looking like an MFID, ORCID, or email. The read-only staging audit on 2026-10-06 found none across 6,850 projects, 45 instruments, and 3,034 users, and every service account has an MFID identity. Run the same audit against production before deploying:

```sql
SELECT project_id FROM project
WHERE project_id ~ '^[0-9a-hjkmnp-tv-z]{26}$'
   OR project_id ~ '^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$'
   OR project_id LIKE '%@%';
SELECT instrument_id FROM instrument
WHERE instrument_id ~ '^[0-9a-hjkmnp-tv-z]{26}$'
   OR instrument_id ~ '^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$'
   OR instrument_id LIKE '%@%';
SELECT username FROM "user"
WHERE username ~ '^[0-9a-hjkmnp-tv-z]{26}$' OR username LIKE '%@%';
SELECT unique_id, username FROM "user"
WHERE is_service_account AND unique_id !~ '^[0-9a-hjkmnp-tv-z]{26}$';
```

Every query should return no rows. `POST /service_accounts` only accepts an MFID `unique_id`, so new service accounts always have one; a legacy service account with a non-MFID `unique_id` would resolve only by username through `{service_account_ref}`.

Legacy slugs longer than 25 characters (48 projects and 2 instruments in staging) remain addressable because resolution falls back to slug matching for anything that is not a valid MFID.

## Deferred work

- `AssociatedFileRead` calls the file identifier `mfid` instead of `unique_id` and exposes both `dataset_id` (internal) and `dataset_mfid`. A future compatibility plan should standardize those public names.
- `GET /access_groups/{group_name}` and `/users/{user_ref}/access_groups/{group_name}` take raw access-group names and remain administrator tools outside this convention.

## Related documentation

- Route index: [`app/api/routes/readme.md`](../app/api/routes/readme.md)
- Permission migration API changes: [`docs/permission_system_api_changes.md`](permission_system_api_changes.md)
