# GET query contract

Status: deployed to production on 2026-09-15 from `main` commit `24a24ab9ce74b01f841750df7003091ce4b57119` with database revision `3f2b6a9c1d84`.

The generated OpenAPI document defines the complete parameter schemas. This document records shared behavior and compatibility decisions. Any contract change here must also be reflected in the API implementation, tests, generated OpenAPI, `docs/hub_api_handoff_review.md`, and the Hub handoff document before the Hub consumes it. Do not update the Hub repository without explicit authorization.

## Common collection behavior

Dataset, sample, project, and instrument collections use endpoint-specific query models and reject unknown parameters with 422. They share these controls where applicable:

- `accessible_to_user` and `accessible_to_project` constrain effective access. Platform contributors and administrators may select another user or project; ordinary callers remain limited to themselves and projects where they hold standing.
- Repeatable `affiliation=owner` selects resources authoritatively owned by the current caller.
- `owner_id` is the preferred exact person or service-account owner filter. `owner_orcid` remains a deprecated alias on supported routes.
- `project_id`, `project_mfid`, and `project_scope=assigned|shared|all` constrain project relationships on dataset and sample collections.
- `creation_time_gte`, `creation_time_lte`, `modification_time_gte`, and `modification_time_lte` require timezone-aware values.
- `include_total=true` requests an exact authorization-filtered count. `false` suppresses it. Omission preserves each route's previous count behavior.
- `include_metadata`, `include_links`, and `include_owner` control representation expansion without changing row membership or cursor identity.

Repeated values use OR within one filter family. Different filter families compose with AND. Authorization and active-resource filtering are always applied before pagination.

## Sorting

Dataset and sample collections accept `sort=created|updated|name` and `direction=asc|desc`. Omitting `sort` preserves the legacy `unique_id DESC` order. Every order uses `unique_id` as a deterministic tie-breaker, and null primary values sort last.

Projects retain `project_id ASC, unique_id ASC`. Instruments retain `creation_time DESC NULLS LAST, unique_id DESC`.

Mutable sorts are live views. An item can move between pages if its name or modification time changes during traversal. Clients should restart pagination after a mutation or refresh.

## Pagination

All four collections retain offset pagination and support opaque cursor pagination. Dataset and sample routes also accept legacy raw MFID cursors only with the default order. New responses emit signed versioned cursors.

Cursors bind the continuation position to the authenticated principal, resource kind, sort, direction, and normalized result filters. Changing that context returns 422. Page size, representation expansion, and `include_total` may change while following a cursor. Every page reapplies authorization.

If both cursor and offset are supplied, cursor takes precedence for compatibility. One extra row is queried to determine whether `next_cursor` is present. A null cursor is the only end-of-collection signal.

## Anchored pages

Dataset and sample collections accept `anchor_mfid` to start an inclusive page at a resource in the requested authorized, filtered, and ordered collection.

- `anchor_mfid` cannot be combined with a cursor or an explicitly supplied offset.
- Missing, inaccessible, deleted, wrong-type, or filter-excluded anchors return 404.
- The first item is the anchor and the page continues in the requested direction.
- The returned `next_cursor` is an ordinary continuation cursor. Do not resend the anchor when following it.
- A client can obtain both sides of a local sequence with two anchored requests using identical filters and opposite directions, then reverse one side and remove the duplicate anchor.

Anchoring is supported only for datasets and samples.

## Facets and null filters

`GET /datasets/facets` and `GET /samples/facets` return authorization-filtered selectable values and counts. They do not return grouped resources or provide a general aggregation language.

Dataset fields:

- `session`
- `measurement`
- `data_format`
- `owner`
- `instrument`
- `project`

Sample fields:

- `sample_type`
- `owner`
- `project`

Text values are preserved exactly. Every bucket returns an optional canonical `label`. Text-facet labels equal their stored values. Owner labels use nonblank full name, username, then stable identifier. Project labels use nonblank title, project ID, then MFID. Instrument labels use nonblank instrument name, instrument ID, then MFID. Unresolved relationships use the raw stored value. Null buckets have null labels and always sort after concrete values. Owner, project, and instrument buckets also include stable identifiers and public-safe resolved references when available.

Facet endpoints accept `sort=value|label|count` and `direction=asc|desc`. The default `sort=value&direction=asc` preserves raw stored-value ordering. Label ordering uses a case-insensitive canonical label key. Count ordering uses the requested count direction. Label and count ties break by raw stored value ascending, and null remains last. Resolved labels do not affect value or count ordering.

Facet pages use bounded opaque cursor pagination. Continuation requests must resend the same field, filters, sort, and direction; the limit may change. Cursors are bound to the authenticated principal and complete result-shaping query context, and authorization is reapplied on every request. Pagination is a live view, so changes to resources or authorization between requests can move buckets and may cause duplicates or omissions.

Dataset and sample list routes expose matching `*_is_null` filters. Omission does not constrain nullability, `true` selects null values, and `false` selects non-null values. Combining `true` with a concrete value for the same field returns 422.

Calendar facets remain deferred. The producer-supplied dataset `timestamp` is unconstrained text and is unsuitable for reliable server-side calendar grouping. A future calendar facet should use API-owned `creation_time` and explicit UTC semantics.

## Singleton GETs

Singleton routes keep explicit representation controls such as `include_metadata`, `include_links`, `include_owner`, `include_members`, and the deprecated sample `include_datasets`. Collection filters, sorting, and pagination do not apply to singleton routes. Unknown parameters return 422.

## Compatibility

- Requests without new parameters preserve previous results and ordering.
- Offset pagination remains supported.
- Existing raw dataset and sample MFID cursors remain supported for the legacy default order.
- Legacy owner and name filters used by known clients remain explicit and deprecated in OpenAPI.
- Preferred and deprecated owner aliases may be combined only when they resolve to the same identity.
- Exact owner filters cannot be combined with current-caller affiliation.
- Path parameter names were not changed in this compatibility release.

## Lifecycle timestamps

Sorting and lifecycle time filters use API-owned `creation_time` and `modification_time`, stored as nullable PostgreSQL `TIMESTAMPTZ`. See [resource timestamp policy](resource_timestamp_policy.md).

## Deferred features

- A unified dataset and sample collection with one global cursor
- Additional project and instrument sort and filter parity
- Cursor conversion for every remaining collection
- Removal of offset pagination, legacy exact filters, or raw cursors
- Generic query languages, arbitrary field selection, or broad expansion syntax
- Previous-page cursors, absolute range metadata, and snapshot pagination
- Project and instrument related-resource counts
- Parent `modification_time` propagation

Project and instrument `modification_time` records direct changes to that resource. A future related-activity feature should use a separately defined `last_activity_time` or activity resource. If related-resource counts are added, they should be explicit opt-in bulk aggregates with documented authorization and relationship semantics.
