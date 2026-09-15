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

### Pagination and ordering were inconsistent

Datasets and samples use cursor and offset compatibility with their default and explicit sort orders. Project and instrument collections now provide cursors over their existing deterministic orders while retaining offset compatibility. Files, users, members, and workflow collections remain offset-paginated. Search endpoints use deterministic relevance ordering but no continuation. A cursor should be added to another endpoint only after its complete ordering and compatibility behavior are defined.

### Singleton expansion parameters are reasonable

Canonical and generic singleton reads use strict query models with explicit `include_metadata`, `include_links`, `include_owner`, `include_members`, and compatibility `include_datasets` flags. Unsupported singleton parameters return 422. These flags remain explicit rather than being replaced by a generic expansion language. Collection filters do not belong on singleton reads because the path already identifies the resource.

## Compatibility policy

- Requests without new parameters preserve current results and ordering.
- Existing documented query parameters retain their names and behavior.
- Legacy exact filters observed in working clients become explicit deprecated parameters in OpenAPI before eventual removal.
- Existing offset pagination remains available.
- Existing cursor precedence is preserved when cursor and offset are supplied together. The cursor controls page position and the offset is ignored.
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

- Dataset, sample, and instrument collections and search endpoints accept `owner_id` as the preferred exact stable identity filter for either a human or service account.
- Dataset and sample `owner_orcid` remains an explicit deprecated exact filter.
- Instrument `owner` remains an explicit deprecated exact filter.
- These filters continue to support existing administrative and Graph Explorer workflows.
- A preferred owner filter and its deprecated alias may be combined only when their values match.
- `affiliation=owner` cannot be combined with an exact owner filter. The API returns 422 rather than guessing whether the caller intended intersection or substitution.

### Sample and dataset sorting

Add these parameters to `GET /samples` and `GET /datasets`:

| Parameter | Type | Values | Default |
| --- | --- | --- | --- |
| `sort` | optional enum | `created`, `updated`, `name` | omitted legacy ordering |
| `direction` | optional enum | `asc`, `desc` | `desc` when sort is present |
| `anchor_mfid` | optional string | canonical dataset or sample MFID | omitted |

This first delivery intentionally excludes type, project, and owner sorting. Those require additional nullable expressions or identity joins and should be added only after the minimum sorts are verified with production-scale query plans.

Mappings:

| Sort | Samples | Datasets |
| --- | --- | --- |
| `created` | Resource creation time | Resource creation time |
| `updated` | Resource modification time with creation time fallback | Resource modification time with creation time fallback |
| `name` | Case-normalized sample name | Case-normalized dataset name |

When sort is omitted, the endpoint retains the existing `unique_id DESC` order and accepts legacy raw MFID cursors. Explicit `sort=created` uses the authoritative resource creation time because clients may provide a preassigned MFID whose lexical order does not represent API creation time. `direction` without `sort` returns 422. Updated, created, and name ordering apply the selected primary value, then `unique_id` in the same direction. Missing primary values sort last in both directions. Name sorting uses one documented case-normalization and database collation strategy across both resource types.

Creation and modification query parameters use timezone-aware datetime validation rather than accepting arbitrary strings. Revision `8c4f2a1d7e60` converts the API-owned resource creation and modification columns from ISO strings to nullable PostgreSQL `TIMESTAMPTZ` columns with database defaults. Revision `3f2b6a9c1d84` enforces database-generated lifecycle values, immutable creation times, and automatically advanced modification times. New resources receive API-generated UTC values, while missing legacy values remain null rather than receiving invented history. The read-only audit reports null, timezone-less, invalid, non-UTC, and UTC values separately, and any unparseable non-null timestamp blocks migration. Timezone-less legacy values are interpreted in `America/Los_Angeles`, missing modification times are repaired from creation times when available, and legacy values with explicit offsets are preserved as instants. Non-null API responses normalize both fields to fixed-microsecond UTC RFC 3339 values ending in `Z`. The producer-supplied dataset `timestamp` field remains an unconstrained domain value and is not part of this lifecycle timestamp policy.

Sorting happens after authorization and filtering and before offset, anchor, or cursor pagination. Invalid sort values return FastAPI's standard 422 validation response. Sort and direction appear as enums in OpenAPI.

### Explicit collection filters

Replace dynamic database-attribute inspection with declared query parameters. The initial query models preserve these dataset and sample filter families:

- Access selectors: `accessible_to_user`, `accessible_to_project`
- Ownership: repeatable current-caller `affiliation`, preferred exact `owner_id`, and deprecated resource-specific owner alias
- Project relation: `project_id`, `project_mfid`, `project_scope`
- Cross-resource relation: `sample_mfid` or `dataset_mfid`
- Dataset instrument: `instrument_mfid`
- Visibility: `visibility`
- Time bounds: creation and modification lower and upper bounds
- Expansions: metadata, links, owner, and deprecated sample datasets
- Pagination: limit, cursor, offset, optional inclusive anchor, and optional exact-total control
- Sorting: sort and direction
- Explicit legacy exact resource fields currently supported by Nano or known clients
- Dataset keyword filtering, which is currently implemented separately from model-column filtering

Instrument and project collections receive explicit models for their currently supported parameters and `affiliation`. Instrument status becomes an explicitly typed enum on the collection instead of a hidden prose-only filter.

Project collections accept opaque cursors over their existing `project_id ASC` order with canonical unique ID as a deterministic tie-breaker. Instrument collections accept opaque cursors over their existing `creation_time DESC NULLS LAST, unique_id DESC` order. Omitting cursor preserves existing ordering, offset behavior, and totals. When cursor and offset are supplied together, cursor takes precedence consistently with dataset and sample collections.

Do not automatically expose every persistence column. A filter remains public only when it has a stable client meaning, safe validation, and tested composition with authorization and pagination.

### Dataset and sample facets

Add authorization-scoped `GET /datasets/facets` and `GET /samples/facets` collection operations. These endpoints return distinct selectable values and counts, not grouped resource records or general statistical aggregations.

Dataset facet fields are `session`, `measurement`, `data_format`, `owner`, `instrument`, and `project`. Sample facet fields are `sample_type`, `owner`, and `project`. The field allowlists are explicit enums and do not expose arbitrary persistence columns.

Text facets preserve stored values exactly. The API does not trim, case-fold, relabel, or merge values, so `XAS`, `xas`, an empty string, and null remain distinct buckets. Null is represented as JSON null and appears after concrete values. Clients provide contextual labels such as "Unspecified" or "No registered instrument" rather than relying on an API sentinel string.

Owner buckets contain the stable stored owner identifier and its public-safe resolved user reference when available. Project and instrument buckets contain the canonical MFID and the existing resolved `ProjectReferenceRead` or `InstrumentReferenceRead`. A null dataset instrument bucket means that no canonical registered instrument is assigned, even when a legacy free-text instrument label remains on the dataset.

Facet counts include only active resources readable by the authenticated caller after the supplied access, affiliation, visibility, project, relationship, lifecycle-time, exact-value, and null filters are applied. The endpoints accept only filters relevant to their facet contract and reject unknown parameters. They do not accept resource ordering, response expansions, offsets, or exact-total controls.

Buckets use raw stored-value ascending order with null last and bounded `limit` plus opaque cursor pagination. Cursors are signed, bound to the caller, resource type, facet field, and normalized filters, and are not valid on resource-list endpoints. Changing the page limit while following a cursor is allowed.

The corresponding dataset and sample collection routes expose optional field-specific `*_is_null` booleans. Omission does not constrain nullability, true selects null values, and false selects non-null values. A true null selector conflicts with a concrete selector for the same field and returns 422.

Month or year facets remain deferred. A future lifecycle-time facet should use API-managed `creation_time` in UTC and a name such as `created_month`. The producer-supplied `timestamp` field remains unsuitable for server-side calendar grouping because its format and timezone are not enforced.

### Singleton GET parameters

Keep current explicit representation controls. Standardize their descriptions, defaults, authorization behavior, and strict unknown-parameter validation in OpenAPI. Do not add ownership, visibility, access-selector, sort, or pagination parameters to singleton reads.

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

Compose endpoint-specific models rather than one model containing irrelevant fields. Model validators and shared pagination parameter-presence checks enforce incompatible combinations, matching slug and MFID pairs, legacy alias agreement, cursor compatibility, and expansion restrictions.

Shared dataset and sample pagination mechanics live in `app/services/resource_pagination.py`. The service validates page-position combinations, resolves configured sort expressions, applies anchor or cursor boundaries, orders and bounds the query, and emits continuation cursors. Routes continue to own authorization, resource filters, relationship expansion, and response serialization.

Repeated ordinary values use OR semantics within one filter family, while different filter families compose with AND. Existing access selectors retain their documented AND semantics. Every repeated parameter has a bounded item count, and blank identity values are rejected or normalized consistently before cursor fingerprinting.

If the FastAPI upgrade is not acceptable after compatibility testing, explicitly declare every parameter on the affected routes and postpone unknown-parameter rejection. Do not build a general custom query binding framework when a native framework mechanism is available.

## Versioned cursor design

Dataset, sample, project, and instrument collection responses emit an opaque base64url-encoded, HMAC-signed, versioned cursor. The transmitted cursor payload is an internal Protobuf value containing only:

- Signing key identifier
- Last normalized primary value when the order has one in addition to the unique ID
- Last unique ID

The cursor contract version is carried by its prefix. Resource kind, sort, direction, normalized result-filter fingerprint, and non-reversible authenticated-principal fingerprint are bound as non-transmitted HMAC context. Changing any of them invalidates the cursor without exposing them in the token. Do not include credentials, user profiles, raw search text, authorization claims, or sensitive metadata. Cursor values are parsed into typed structures and used only through allowlisted bound SQL expressions. The signature detects modification but does not grant access, and every page reapplies authorization. Cursor signing supports an explicit key identifier and rotation policy. `CRUCIBLE_CURSOR_SIGNING_KEYS` is a JSON object mapping key IDs to secrets and `CRUCIBLE_CURSOR_ACTIVE_KEY_ID` selects the key used for new cursors. Rotation first adds a new key while retaining the old key, then changes the active key ID. Removing an old key invalidates outstanding cursors signed by it and must wait for the documented client pagination window. A single `CRUCIBLE_CURSOR_SIGNING_KEY` is supported as a deployment fallback, and deployments without either cursor setting fall back to the API authentication secret.

The filter hash binds the cursor to affiliation, project, relationship, visibility, time, and exact-field filters. Preferred and deprecated owner aliases normalize to the same canonical owner identity. It excludes representation-only expansions, page size, offset, and `include_total` so a caller can change `limit`, expansion flags, or total-count preference while following the same result set. Using a cursor with a different principal, resource type, sort, direction, or result-shaping filter returns 422.

Dataset and sample collections accept `anchor_mfid` on a request without `cursor` or an explicitly supplied `offset`. The anchor must be a readable active resource in the collection after every supplied scope and result filter is applied. A missing, inaccessible, wrong-type, deleted, or filtered-out anchor returns 404. The returned page starts with the anchor and continues inclusively in the requested deterministic ordering. The anchor uses the same normalized primary value and unique-ID tie-breaker as cursor pagination, including null-last ordering. It does not change the result set or cursor identity, so the returned `next_cursor` can be followed without resending `anchor_mfid`. Two requests using the same anchor, filters, sort, and opposite directions can provide the two sides of a local navigation sequence after the client reverses one side and removes the duplicate anchor.

For a non-null ascending primary value, the keyset predicate is conceptually:

```sql
WHERE primary_value > :last_primary
   OR (primary_value = :last_primary AND unique_id > :last_unique_id)
ORDER BY primary_value ASC NULLS LAST, unique_id ASC
```

Descending ordering reverses both comparisons. Nullable ordering includes an explicit null rank so missing values remain last across page boundaries.

Legacy raw MFID cursors continue to mean `unique_id < cursor` only on dataset and sample collections when sort is omitted and the legacy `unique_id DESC` compatibility order applies. Project and instrument collections accept only signed cursors because they had no legacy cursor contract. Emit only the new cursor format after deployment so active old clients can continue echoing cursors without understanding their contents. Query one row beyond the requested limit to determine whether another page exists, return only the requested rows, and set `next_cursor` to null when the extra row is absent.

Keyset pagination is a live view. The unique-ID tie-breaker makes a fixed set deterministic, but an item can move between pages if its name or modification time changes during traversal. The API documents this behavior, and interactive clients restart from the first page after mutations or refreshes. Snapshot pagination is deferred to a bulk synchronization or export contract.

`total` remains an exact authorization-filtered count on the first or anchored dataset or sample page and is omitted by default from cursor continuations and deep offset responses. Project and instrument collections retain their existing default count behavior. Every collection accepts `include_total=true` to request an exact count and `include_total=false` to suppress it. Omitting the flag preserves each endpoint's legacy behavior. Full pagination links and snapshot metadata remain deferred.

## Query execution order

All collection implementations follow this logical pipeline:

```text
authorized rows -> active/deletion policy -> declared filters -> deterministic ordering -> offset, anchor, or cursor boundary -> representation expansion
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
8. Implemented: preserve cursor precedence over offset, add project and instrument cursors over their existing deterministic orders, and add `include_total` plus preferred exact `owner_id` controls without removing legacy aliases.
9. Implemented: add inclusive `anchor_mfid` placement to dataset and sample collections without changing continuation cursors.
10. Pending deployment evidence: run the timestamp audit and capture representative PostgreSQL query plans before adding any indexes.
11. Completed: refresh route documentation, `CHANGELOG.md`, and generated OpenAPI deterministically.
12. After API deployment: update the Hub generated client and enable Owned plus type-specific table sorting.
13. After API contract stabilization: update Nano signatures to expose the new typed parameters and deprecate unrestricted collection `**kwargs` without removing them in the same release.
14. Before merge: verify Crucible Lens and Graph Explorer against their existing queries.

## Timestamp migration deployment

The timestamp schema change uses a coordinated direct migration while production pods remain running. Follow [the resource timestamp migration runbook](resource_timestamp_online_migration_runbook.md).

1. Run `migrations/data/20260908_audit_resource_timestamps.py` against the target database and stop for any unsupported format.
2. Capture the required backup and prepare the reviewed main deployment behind its expected-schema readiness check.
3. Apply through revision `3f2b6a9c1d84` during low traffic without scaling production pods down.
4. Complete the rolling application deployment immediately after the migration commits.
5. Exercise default, owner-affiliated, created, updated, and name collection queries and capture `EXPLAIN ANALYZE` plans on representative data.
6. Add indexes in a later reviewed migration only if the captured plans justify them.

## Required tests

- No-parameter requests preserve current default results and newest-first order.
- Every declared filter composes with authorization and deletion filtering.
- Known Nano, Lens, Hub, and Graph Explorer parameter combinations remain valid.
- Deprecated exact owner and name filters retain current behavior.
- Unknown and misspelled parameters return 422 on the modernized collection, search, canonical singleton, and generic resource singleton routes.
- `affiliation=owner` returns only resources authoritatively owned by the caller for all four resource types.
- Repeated affiliation values use OR semantics, and unsupported values return 422.
- `affiliation=owner` cannot be combined with an exact owner selector.
- Cursor takes precedence when cursor and offset are supplied together.
- Every supported sort works in both directions.
- Equal and null primary values paginate without duplicates or omissions.
- A final page containing exactly `limit` rows returns no next cursor.
- Case-normalized name ordering is deterministic.
- Default raw MFID cursors remain accepted.
- Project and instrument collections paginate through their existing orders without duplicates or omissions, including null instrument creation times.
- Project and instrument no-cursor requests preserve existing offset, ordering, and total behavior.
- Project and instrument collections reject raw cursor values because those routes had no legacy raw-cursor contract.
- New cursors reject changed result filters, resource kinds, sorts, and directions.
- New cursors reject tampering and use by a different authenticated principal.
- Changing expansion flags or page size does not invalidate a cursor.
- Changing `include_total` does not invalidate a cursor, and the flag controls exact-count calculation without changing row membership.
- Offset and cursor pages apply the same selected ordering.
- Anchored dataset and sample pages start with the anchor in both directions and preserve equal-primary tie-breaking and null-last ordering.
- Anchors excluded by authorization, lifecycle policy, resource type, or supplied filters return 404.
- `anchor_mfid` rejects cursor and explicitly supplied offset combinations, while anchored continuation cursors work without resending the anchor.
- Explicit `sort=created` uses resource creation time, while omitted sort preserves legacy unique-ID order.
- Time bounds reject malformed or timezone-ambiguous values.
- Concurrent changes to mutable sort values follow the documented live-view behavior.
- Generated OpenAPI exposes all supported parameters, enum values, defaults, deprecations, and validation constraints.

## Explicitly out of scope for the first delivery

- Unified sample and dataset collection
- Globally sorted Hub All view
- Type, project, or owner sorting
- Dataset and sample sorting and time-filter parity for project and instrument collections
- Cursor conversion for every remaining API collection
- Removal of offset pagination
- Removal of legacy exact filters or raw cursors
- Path parameter renaming
- Generic query languages, arbitrary field selection, broad expansion syntax, or client-provided SQL field names
- Changes to singleton response shapes
- ETag and `If-None-Match` conditional singleton reads
- Optional dataset, sample, or other related-resource counts on project and instrument responses
- Project lifecycle status and status filtering
- Full pagination links and snapshot pagination
- Parent activity propagation through `modification_time`

Project and instrument `modification_time` continues to describe a direct mutation of that resource. Creating, updating, linking, unlinking, or deleting a child dataset or sample does not modify its parent project's or instrument's lifecycle timestamp. A future activity feature should use a separately named `last_activity_time` derived from durable activity events or a purpose-built aggregate, with explicit event coverage, authorization, retention, ordering, and backfill semantics.

If project or instrument counts are later required, expose them through an explicit opt-in such as `include_counts=true`, compute them with bounded bulk aggregates rather than per-row queries, and define whether project counts cover primary assignment only or also shared access. Instrument counts should count assigned datasets only. Counts must include only resources visible under the documented permission and deletion policy.
