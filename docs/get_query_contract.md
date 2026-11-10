# Collection queries

This page describes the filtering, sorting, pagination and facet behavior that list routes share. The OpenAPI document defines each parameter.

## Validation

Dataset, sample, project and instrument collections, `GET /users/{user_ref}/projects`, the dataset and sample facet routes, the dataset, sample, project and instrument search routes, and singleton routes reject unknown query parameters with 422. `GET /users` ignores unknown parameters, logs them and adds the header `Deprecation: @1799280000`.

Repeated values of one filter use OR semantics unless stated otherwise; different filters combine with AND. Authorization and the exclusion of records pending deletion apply before pagination.

## Filters

These filters apply to the dataset, sample, project and instrument collections where each is declared:

- `accessible_to_user` and `accessible_to_project` (at most 10 values each, AND semantics) keep records that every listed user or project can access. Ordinary callers may select only themselves and projects where they hold standing; platform contributors and administrators may select any user or project.
- `affiliation` relates each result to the caller: `owner` and `member` on datasets and samples, `owner` on projects, `owner` and `maintainer` on instruments. `member` matches records reached through the caller's own grants, so records visible only because they are public are excluded. `affiliation` cannot be combined with an exact owner filter (422).
- `mfid` (at most 100 values) selects records by MFID for batch lookups. MFIDs the caller cannot read, missing MFIDs and records pending deletion are omitted, and results keep the collection's order, so clients key them by MFID. On projects it finds projects the caller is not a member of; on instruments it searches every status unless `status` is given. On datasets and samples `unique_id` is a deprecated alias of `mfid` (header `Deprecation: @1792540800`); sending both returns 422.
- `owner_id` is the exact owner filter and accepts a `unique_id`, a linked ORCID, a username or an email. Deprecated aliases: `owner_orcid` on datasets and samples, `owner` on instruments. An alias combined with `owner_id` must name the same owner (422).
- `project_id` and `project_mfid` select one project on datasets, samples, their facets and search routes; `project_scope=assigned|shared|all` (default `assigned`) chooses the relationship and requires one of them. Datasets also accept `instrument_id` and `instrument_mfid`; `instrument_name` is deprecated.
- `visibility=all|public|private` restricts datasets and samples by public visibility.
- `creation_time_gte`, `creation_time_lte`, `modification_time_gte` and `modification_time_lte` are inclusive. A value without an offset is read as UTC. A lower bound after its upper bound returns 422.
- Datasets and samples expose `*_is_null` filters for the faceted nullable fields (for example `measurement_is_null`, `owner_id_is_null`, `project_mfid_is_null`). Omission does not constrain nullability, `true` selects null values and `false` non-null values. `true` combined with a value for the same field returns 422.
- On projects, `member_ref` (at most 10 values, AND semantics) keeps projects where every listed user holds a membership; it accepts an ORCID, service-account MFID, username or email, follows the same selection rule as `accessible_to_user`, and excludes projects reached only through resource grants. `member_role` restricts those memberships to the listed roles and requires `member_ref`.
- Projects and instruments accept `active_since`. Activity is recorded at most hourly.
- Instruments default to `status=active` unless `status`, `instrument_id` or `mfid` is given.

`include_metadata`, `include_links`, `include_owner` and `include_members` change the representation only, never the rows or the cursor. `include_members` on a collection requires an exact `project_id` or `instrument_id`. Project and instrument items include `role`: the caller's project membership role, or the caller's effective role on the instrument record. Public access is not a role, and bound instrument service accounts receive `null`.

## Sorting

| Collection | `sort` values | Default order |
|---|---|---|
| `GET /datasets`, `GET /samples` | `created`, `updated`, `name` | `unique_id` descending |
| `GET /projects`, `GET /users/{user_ref}/projects` | `project_id`, `title`, `created`, `updated`, `activity` | `project_id` ascending, then `unique_id` ascending |
| `GET /instruments` | `instrument_id`, `name`, `created`, `updated`, `activity` | `creation_time` descending, nulls last, then `unique_id` descending |
| `GET /users` | `name`, `username`, `unique_id`, `email`, `last_active` | `name` ascending |

`direction` is `asc` or `desc`. On datasets and samples it defaults to `desc`. On projects and instruments an explicit sort defaults to `desc` for `created`, `updated` and `activity` and to `asc` otherwise. On users it defaults to `asc`. `direction` without `sort` returns 422 on datasets, samples, projects and instruments.

Every order uses `unique_id` as the final tie-breaker and places null values last. `updated` uses `modification_time`, falling back to `creation_time`. `name` on datasets, samples and instruments, project `title`, `instrument_id` and every user sort are case-insensitive; project `project_id` sorts by the stored slug. User `name` sorts by last name, then first name. `email` and `last_active` user sorts, and the `active_since`, `inactive_since` and `never_active` user filters, are restricted to platform administrators (403).

Mutable sorts are live views: an item can move between pages when its name or modification time changes during traversal. Clients restart pagination after a mutation.

## Pagination

Responses use the envelope `{total, limit, offset, items, next_cursor}`. A null `next_cursor` is the only end-of-collection signal.

| Pagination | Routes | `limit` default and maximum |
|---|---|---|
| Cursor, with `offset` accepted | `GET /datasets`, `GET /samples`, `GET /projects`, `GET /instruments`, `GET /users`, `GET /users/{user_ref}/projects`, `GET /join_requests`, `GET /deletion_requests`, `GET /deletion_audit`, `GET /embeddings` | 100, 1000 |
| Cursor only | `GET /account/apikeys`, `GET /account/sessions`, `GET /account/auth-events` and their `/users/{user_ref}/...` counterparts | 50, 200 |
| Cursor only | `GET /datasets/facets`, `GET /samples/facets` | 100, 1000 |
| Cursor only | `GET /join_requests/facets` | 50, 200 |
| Offset only | `GET /files`, `GET /ingestion_requests`, `GET /postprocessing_runs`, `GET /service_accounts`, `GET /projects/{project_ref}/users`, `GET /datasets/{child_dataset_mfid}/parents`, `GET /datasets/{parent_dataset_mfid}/children`, `GET /samples/{child_sample_mfid}/parents`, `GET /samples/{parent_sample_mfid}/children`, `GET /account/join_requests` (deprecated) | 100, 1000 |
| One page, no continuation | `GET /datasets/search`, `GET /samples/search`, `GET /projects/search`, `GET /instruments/search`, `GET /users/search`, `GET /resources/metadata/search` | 20, 50 |

Search routes require `q` of at least 3 characters.

Cursors are opaque and signed. A cursor is bound to the authenticated principal, the collection, the sort, the direction and the result filters; changing any of them returns 422. `limit`, the representation parameters and `include_total` may change between pages. Every page reapplies authorization. When both `cursor` and `offset` are sent, `cursor` takes precedence. Offset pagination on `/datasets` and `/samples` is deprecated. In the default order, dataset and sample routes also accept a bare MFID as a cursor, retained for older clients.

`include_total=true` computes an exact, authorization-filtered `total` and `false` omits it. When it is omitted, datasets and samples return `total` only on the first page (no cursor, `offset=0`), while projects, instruments, `GET /users` and `GET /users/{user_ref}/projects` always return it.

## Anchored pages

`GET /datasets` and `GET /samples` accept `anchor_mfid` to start an inclusive page at one record of the authorized, filtered and ordered collection.

- `anchor_mfid` cannot be combined with `cursor` or an explicit `offset` (422).
- A missing, unreadable, pending-deletion or filter-excluded anchor returns 404.
- The first item is the anchor and the page continues in the requested direction. The returned `next_cursor` is an ordinary cursor; do not resend the anchor with it.
- Two anchored requests with identical filters and opposite directions return both sides of a record; reverse one side and remove the duplicate anchor.

## Facets

`GET /datasets/facets` and `GET /samples/facets` return authorization-filtered values and counts for one `field`. They do not return records.

| Route | `field` values |
|---|---|
| `GET /datasets/facets` | `session`, `measurement`, `data_format`, `owner`, `instrument`, `project` |
| `GET /samples/facets` | `sample_type`, `owner`, `project` |

Each bucket has `value`, `count` and a canonical `label`. Text labels equal their stored values. Owner labels use the nonblank full name, then username, then stable identifier; project labels the nonblank title, then project ID, then MFID; instrument labels the nonblank instrument name, then instrument ID, then MFID. Unresolved relationships use the raw stored value. The null bucket has a null label. Owner, project and instrument buckets also include stable identifiers and public-safe references when available.

Facet routes accept `sort=value|label|count` (default `value`) and `direction=asc|desc` (default `asc`). Label ordering is case-insensitive. Label and count ties break by raw value ascending, and the null bucket always sorts last. A continuation request must resend the same field, filters, sort and direction; `limit` may change. Facet pages are live views, so changes between requests can move buckets.

## Users

`GET /users` declares `unique_id`, `username`, `email`, `first_name`, `last_name`, `q`, `search_fields`, `permissive`, `is_service_account`, `pending`, `platform_role`, `active_since`, `inactive_since`, `never_active`, `include_total`, `limit`, `cursor`, `offset`, `sort` and `direction`.

- Exact `unique_id`, `username` and `email` filters search every user and return the same representation as `GET /users/{user_ref}`. `unique_id` and `username` are repeatable (at most 100 values each).
- Without an exact filter, platform administrators receive the full directory with email and platform roles; other callers receive public-safe profiles of users who share an access group with them.
- `first_name` and `last_name` are case-insensitive substrings with `permissive=true` (default) and exact otherwise. `permissive` does not affect the exact filters.
- `q` is a case-insensitive substring search over first name, last name, username, unique ID and, for platform administrators, email. Repeatable `search_fields` restricts `q` to the named fields and requires it.
- `platform_role`, `pending`, the activity filters and `email` in `search_fields` are restricted to platform administrators (403).

`user_capabilities` is set on `GET /users/{user_ref}` and every `GET /users` item. Platform administrators also receive `api_key_status` on each item that has a key.

## Singleton routes

Singleton routes accept only representation parameters such as `include_metadata`, `include_links`, `include_owner`, `include_members` and the deprecated sample `include_datasets` (replaced by `include_links`).

## Removed routes

`GET /users/{user_ref}/datasets` and `GET /users/{user_ref}/samples` were removed in 4.0.0 and return 410 `route_removed`; use `GET /datasets?accessible_to_user=` and `GET /samples?accessible_to_user=`. `GET /datasets/{dataset_mfid}/samples` was removed; use `GET /samples?dataset_mfid=`.

Sorting and time filters use `creation_time` and `modification_time`; see [Timestamps](resource_timestamp_policy.md).
