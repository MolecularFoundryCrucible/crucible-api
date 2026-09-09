# GET query contract modernization

Status: Implemented, pending review and deployment validation

Branch: `feature/get-query-contract`

Sources:

- Hub API handoff in `/home/roncofaber/software/crucible-hub/docs/crucible-api-handoff.md`
- Sortable collection handoff reviewed from `/tmp/crucible-api-data-sorting-handoff.md`
- Current generated OpenAPI artifact and route implementations
- Known Nano Crucible, Crucible Hub, Crucible Lens, and Crucible Graph Explorer callers

## Objective

Make collection and singleton GET contracts explicit, validated, deterministic, safe to paginate, and usable from generated clients without breaking currently working callers. The first delivery adds current-caller affiliation filtering and server-side sorting to sample and dataset collections, removes dependence on hidden model-column filtering, and establishes reusable query and cursor primitives for later collection improvements.

Any decision or implementation change in this document must also update `/home/roncofaber/software/crucible-hub/docs/crucible-api-handoff.md` and `docs/hub_api_handoff_review.md`.

## Current findings

### Ownership filtering exists only as inconsistent hidden behavior

Dataset and sample collections inspect raw query parameters and apply any key matching a model attribute. This makes `owner_orcid`, `sample_name`, `dataset_name`, and other database fields work even though they are absent from OpenAPI. Instrument collections use a separate allowlist that includes `owner` and other fields but exposes only `instrument_id` in OpenAPI. Project collections can filter effective access through `accessible_to_user`, but cannot distinguish membership from ownership.

The deprecated `/users/{user_id}/datasets` and `/users/{user_id}/samples` routes also return accessible resources rather than resources owned by the user. There is no consistent public contract for a current-account Owned view.

### Hidden filtering is used by working clients

Crucible Graph Explorer calls Nano collection and count methods with `owner_orcid` and uses exact `sample_name` filtering. Nano intentionally forwards documented legacy keyword arguments to dataset and sample collections and uses `instrument_name` for exact instrument lookup. These parameters cannot be rejected without a compatibility period.

### Unknown query parameters can broaden a request silently

Dataset, sample, and instrument collections currently ignore unknown query keys that do not match an accepted model field or allowlist entry. A misspelled scope or identity parameter can therefore return an unfiltered page. Query parameters that affect result scope must fail validation when unknown or invalid.

### Pagination and ordering are inconsistent

Datasets and samples use `unique_id DESC` with cursor and offset compatibility. Projects, instruments, files, users, members, and workflow collections use offset pagination. Search endpoints use deterministic relevance ordering but no continuation. A single generic cursor cannot be applied safely until each endpoint has a total deterministic ordering.

### Singleton expansion parameters are reasonable

Canonical singleton reads use explicit `include_metadata`, `include_links`, `include_owner`, `include_members`, and compatibility `include_datasets` flags. These flags should remain explicit rather than being replaced by a generic expansion language. Collection filters do not belong on singleton reads because the path already identifies the resource.

## Compatibility policy

- Requests without new parameters preserve current results and ordering.
- Existing documented query parameters retain their names and behavior.
- Legacy exact filters observed in working clients become explicit deprecated parameters in OpenAPI before eventual removal.
- Existing offset pagination remains available.
- Cursor and offset pagination are mutually exclusive. Supplying both returns 422 instead of applying silent precedence.
- Existing raw MFID cursors remain accepted for the legacy default ordering. New responses emit a versioned opaque cursor.
- A raw legacy cursor combined with non-default sorting returns 422.
- Existing generated-client path parameter names are not renamed in this compatibility release because OpenAPI name changes can break source compatibility even when the URL is unchanged.
- Unknown query parameters return 422 after every supported legacy parameter has been declared.
- A compatibility alias and its replacement may be supplied together only when they resolve to the same value. Conflicting values return 422.
- Deprecation and removal require an announced release window and a known-client migration check.

## Public contract for the first delivery

### Shared affiliation parameter

Add an optional repeatable `affiliation` enum to:

- `GET /datasets`
- `GET /samples`
- `GET /instruments`
- `GET /projects`
- `GET /datasets/search`
- `GET /samples/search`
- `GET /instruments/search`
- `GET /projects/search`

The first delivery supports `affiliation=owner`. Omitting `affiliation` preserves the current behavior and returns every resource readable by the caller. Repeated affiliation values use OR semantics so later releases can add stable access relations without changing the parameter shape.

With `affiliation=owner`, the result contains only resources owned by the authenticated caller. Authorization, deletion, and all other filters apply before ordering and pagination. Totals and cursors describe the owned subset. The parameter cannot select another user and does not grant access. Platform administrators receive only resources they authoritatively own rather than every resource they can administer.

Ownership must follow authoritative permission state. Dataset, sample, and instrument ownership is the identity principal holding the resource owner grant. Project ownership is the user holding owner standing in the project's own access group. Denormalized owner columns may be used only if tested invariants and a data audit establish that they cannot diverge from the authoritative permission records.

Keep current identity filters for compatibility:

- Dataset and sample `owner_orcid` remains an explicit deprecated exact filter.
- Instrument `owner` remains an explicit deprecated exact filter.
- These filters continue to support existing administrative and Graph Explorer workflows.
- `affiliation=owner` cannot be combined with a legacy owner filter. The API returns 422 rather than guessing whether the caller intended intersection or substitution.

### Sample and dataset sorting

Add these parameters to `GET /samples` and `GET /datasets`:

| Parameter | Type | Values | Default |
| --- | --- | --- | --- |
| `sort` | optional enum | `created`, `updated`, `name` | omitted legacy ordering |
| `direction` | optional enum | `asc`, `desc` | `desc` when sort is present |

This first delivery intentionally excludes type, project, and owner sorting. Those require additional nullable expressions or identity joins and should be added only after the minimum sorts are verified with production-scale query plans.

Mappings:

| Sort | Samples | Datasets |
| --- | --- | --- |
| `created` | Resource creation time | Resource creation time |
| `updated` | Resource modification time with creation time fallback | Resource modification time with creation time fallback |
| `name` | Case-normalized sample name | Case-normalized dataset name |

When sort is omitted, the endpoint retains the existing `unique_id DESC` order and accepts legacy raw MFID cursors. Explicit `sort=created` uses the authoritative resource creation time because clients may provide a preassigned MFID whose lexical order does not represent API creation time. `direction` without `sort` returns 422. Updated, created, and name ordering apply the selected primary value, then `unique_id` in the same direction. Missing primary values sort last in both directions. Name sorting uses one documented case-normalization and database collation strategy across both resource types.

Creation and modification query parameters use timezone-aware datetime validation rather than accepting arbitrary strings. Revision `8c4f2a1d7e60` converts the resource creation and modification columns from ISO strings to PostgreSQL `TIMESTAMPTZ`, and `migrations/data/20260908_audit_resource_timestamps.py` verifies that existing values contain explicit timezones before deployment. This removes the unsafe assumption that lexically ordered timestamp strings are chronologically ordered across offsets.

Sorting happens after authorization and filtering and before offset or cursor pagination. Invalid sort values return FastAPI's standard 422 validation response. Sort and direction appear as enums in OpenAPI.

### Explicit collection filters

Replace dynamic database-attribute inspection with declared query parameters. The initial query models preserve these dataset and sample filter families:

- Access selectors: `accessible_to_user`, `accessible_to_project`
- Affiliation: repeatable `affiliation` and deprecated resource-specific owner alias
- Project relation: `project_id`, `project_mfid`, `project_scope`
- Cross-resource relation: `sample_mfid` or `dataset_mfid`
- Dataset instrument: `instrument_mfid`
- Visibility: `visibility`
- Time bounds: creation and modification lower and upper bounds
- Expansions: metadata, links, owner, and deprecated sample datasets
- Pagination: limit, cursor, and offset
- Sorting: sort and direction
- Explicit legacy exact resource fields currently supported by Nano or known clients
- Dataset keyword filtering, which is currently implemented separately from model-column filtering

Instrument and project collections receive explicit models for their currently supported parameters and `affiliation`. Instrument status becomes an explicitly typed enum on the collection instead of a hidden prose-only filter.

Do not automatically expose every persistence column. A filter remains public only when it has a stable client meaning, safe validation, and tested composition with authorization and pagination.

### Singleton GET parameters

Keep current explicit representation controls. Standardize their descriptions, defaults, and authorization behavior in OpenAPI. Do not add ownership, visibility, access-selector, sort, or pagination parameters to singleton reads.

Retain exact mutable-slug lookup behavior used by current clients. Do not rename OpenAPI path parameter variables in this release. A later major contract may standardize path parameter names and remove deprecated sample `include_datasets` behavior after client migration.

## Query model implementation

Upgrade FastAPI from 0.112.2 to a reviewed version at or above 0.115.0 so routes can use native Pydantic query-parameter models. Run the full test suite and inspect the complete OpenAPI diff as a dedicated first commit because framework upgrades can change validation and schema generation beyond these routes.

Create reusable Pydantic query components with `extra="forbid"`:

- Offset pagination
- Cursor pagination
- Resource expansion flags
- Resource time bounds
- Project relationship scope
- Access selectors
- Affiliation values
- Sort field and direction

Compose endpoint-specific models rather than one model containing irrelevant fields. Model validators and route-level parameter-presence checks enforce incompatible combinations, matching slug and MFID pairs, legacy alias agreement, cursor and offset exclusivity, cursor compatibility, and expansion restrictions.

Repeated ordinary values use OR semantics within one filter family, while different filter families compose with AND. Existing access selectors retain their documented AND semantics. Every repeated parameter has a bounded item count, and blank identity values are rejected or normalized consistently before cursor fingerprinting.

If the FastAPI upgrade is not acceptable after compatibility testing, explicitly declare every parameter on the affected routes and postpone unknown-parameter rejection. Do not build a general custom query binding framework when a native framework mechanism is available.

## Versioned cursor design

New sample and dataset responses emit an opaque base64url-encoded, HMAC-signed, versioned cursor containing:

- Cursor contract version
- Resource kind
- Sort field
- Direction
- Whether the primary value is null
- Last normalized primary value when the sort has one in addition to the unique ID
- Last unique ID
- A hash of normalized result-shaping filters
- A non-reversible authenticated-principal fingerprint

Do not include credentials, user profiles, raw search text, authorization claims, or sensitive metadata. Cursor values are parsed into typed structures and used only through allowlisted bound SQL expressions. The signature detects modification but does not grant access, and every page reapplies authorization. Cursor signing supports an explicit key identifier and rotation policy. `CRUCIBLE_CURSOR_SIGNING_KEYS` is a JSON object mapping key IDs to secrets and `CRUCIBLE_CURSOR_ACTIVE_KEY_ID` selects the key used for new cursors. Rotation first adds a new key while retaining the old key, then changes the active key ID. Removing an old key invalidates outstanding cursors signed by it and must wait for the documented client pagination window. A single `CRUCIBLE_CURSOR_SIGNING_KEY` is supported as a deployment fallback, and deployments without either cursor setting fall back to the API authentication secret.

The filter hash binds the cursor to affiliation, project, relationship, visibility, time, and exact-field filters. It excludes representation-only expansions and page size so a caller can change `limit` or expansion flags while following the same result set. Using a cursor with a different principal, resource type, sort, direction, or result-shaping filter returns 422.

For a non-null ascending primary value, the keyset predicate is conceptually:

```sql
WHERE primary_value > :last_primary
   OR (primary_value = :last_primary AND unique_id > :last_unique_id)
ORDER BY primary_value ASC NULLS LAST, unique_id ASC
```

Descending ordering reverses both comparisons. Nullable ordering includes an explicit null rank so missing values remain last across page boundaries.

Legacy raw MFID cursors continue to mean `unique_id < cursor` only when sort is omitted and the legacy `unique_id DESC` compatibility order applies. Emit only the new cursor format after deployment so active old clients can continue echoing cursors without understanding their contents. Query one row beyond the requested limit to determine whether another page exists, return only the requested rows, and set `next_cursor` to null when the extra row is absent.

Keyset pagination is a live view. The unique-ID tie-breaker makes a fixed set deterministic, but an item can move between pages if its name or modification time changes during traversal. The API documents this behavior, and interactive clients restart from the first page after mutations or refreshes. Snapshot pagination is deferred to a bulk synchronization or export contract.

`total` remains an exact authorization-filtered count on the first page and is omitted from cursor continuation and deep offset responses. Optional total suppression, full pagination links, and snapshot metadata may be added later without changing row membership or cursor semantics.

## Query execution order

All collection implementations follow this logical pipeline:

```text
authorized rows -> active/deletion policy -> declared filters -> deterministic ordering -> page boundary -> representation expansion
```

Ordering joins must not bypass permission predicates or reveal hidden values through validation errors. Count queries and data queries use the same result-shaping predicates. Representation expansions do not affect totals, cursor identity, or row membership.

## Index and performance policy

Do not add speculative indexes. Capture `EXPLAIN ANALYZE` evidence against representative PostgreSQL data for default, owner-affiliated, created, updated, and name queries with common project filters. Add conventional or expression indexes only where the plans demonstrate a need. Candidate expressions include resource timestamps, normalized names, and authoritative owner lookup paths.

The collection limit remains bounded. Expensive metadata, link, and owner expansions receive measured lower limits or a bounded query-complexity rule if production-scale response tests show that the ordinary collection maximum is unsafe.

Cursor queries must avoid deep offset cost for supported orderings. Offset remains a compatibility feature and receives the same ordering but no new scalability guarantee.

## Combined Hub Data view

Sorting the independent sample and dataset collections enables honest sortable type-specific views. It does not make the Hub's combined All view globally sortable because two independently paginated streams cannot be merged into one correct global page from partial results.

Keep the combined All view on a fixed documented order. If global sorting becomes required, design a unified read-only resource collection with a discriminated row model and one cursor across both types. Do not approximate global order in the Hub.

## Delivery sequence

1. Completed: upgrade FastAPI in an isolated commit and verify the framework-only test and OpenAPI baseline.
2. Implemented: add query enums, reusable components, endpoint-specific query models, and unknown-parameter rejection tests.
3. Implemented: replace raw dynamic filtering while preserving explicit deprecated legacy filters.
4. Implemented: add authoritative `affiliation=owner` filtering to collections and matching search endpoints.
5. Implemented: add created, updated, and name sorting to dataset and sample collections.
6. Implemented: add the versioned cursor codec, filter binding, legacy cursor acceptance, lexicographic predicates, and signing-key rotation.
7. Implemented: apply requested ordering consistently to offset pagination.
8. Pending deployment evidence: run the timestamp audit and capture representative PostgreSQL query plans before adding any indexes.
9. Completed: refresh route documentation, `CHANGELOG.md`, and generated OpenAPI deterministically.
10. After API deployment: update the Hub generated client and enable Owned plus type-specific table sorting.
11. After API contract stabilization: update Nano signatures to expose the new typed parameters and deprecate unrestricted collection `**kwargs` without removing them in the same release.
12. Before merge: verify Crucible Lens and Graph Explorer against their existing queries.

## Timestamp migration deployment

The schema change needs a coordinated deployment because application versions that model the resource timestamps as strings and versions that model them as datetimes should not run concurrently.

1. Run `migrations/data/20260908_audit_resource_timestamps.py` against the target database and stop if it reports invalid values.
2. Capture the required backup and pause API writers and old API replicas.
3. Apply Alembic revision `8c4f2a1d7e60`.
4. Deploy only API versions containing the datetime model change before resuming traffic.
5. Exercise default, owner-affiliated, created, updated, and name collection queries and capture `EXPLAIN ANALYZE` plans on representative data.
6. Add indexes in a later reviewed migration only if the captured plans justify them.

## Required tests

- No-parameter requests preserve current default results and newest-first order.
- Every declared filter composes with authorization and deletion filtering.
- Known Nano, Lens, Hub, and Graph Explorer parameter combinations remain valid.
- Deprecated exact owner and name filters retain current behavior.
- Unknown and misspelled parameters return 422.
- `affiliation=owner` returns only resources authoritatively owned by the caller for all four resource types.
- Repeated affiliation values use OR semantics, and unsupported values return 422.
- `affiliation=owner` cannot be combined with a legacy owner selector.
- Cursor and offset supplied together return 422.
- Every supported sort works in both directions.
- Equal and null primary values paginate without duplicates or omissions.
- A final page containing exactly `limit` rows returns no next cursor.
- Case-normalized name ordering is deterministic.
- Default raw MFID cursors remain accepted.
- New cursors reject changed result filters, resource kinds, sorts, and directions.
- New cursors reject tampering and use by a different authenticated principal.
- Changing expansion flags or page size does not invalidate a cursor.
- Offset and cursor pages apply the same selected ordering.
- Explicit `sort=created` uses resource creation time, while omitted sort preserves legacy unique-ID order.
- Time bounds reject malformed or timezone-ambiguous values.
- Concurrent changes to mutable sort values follow the documented live-view behavior.
- Generated OpenAPI exposes all supported parameters, enum values, defaults, deprecations, and validation constraints.

## Explicitly out of scope for the first delivery

- Unified sample and dataset collection
- Globally sorted Hub All view
- Type, project, or owner sorting
- Cursor conversion for every API collection
- Removal of offset pagination
- Removal of legacy exact filters or raw cursors
- Path parameter renaming
- Generic query languages, arbitrary column selection, or client-provided SQL field names
- Changes to singleton response shapes
