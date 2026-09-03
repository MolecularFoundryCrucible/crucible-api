# Database and Query Architecture Review

Date: 2026-09-02

Status: Review draft

## Scope

This document reviews the existing database structure and API query patterns with four goals:

- Efficient PostgreSQL queries and indexes.
- Consistent authorization and transactional behavior.
- Clear SQLAlchemy and SQLModel architecture without unnecessary duplication.
- Backward-compatible improvements to existing routes rather than additional routes.

This is a static code and schema review. It does not include production `EXPLAIN ANALYZE` results or `pg_stat_statements` data. Index recommendations are therefore candidates that must be validated against real query plans and production statistics before migration.

## Overall assessment

The core dataset, sample, search, and permission query architecture is generally sound. The strongest patterns are keyset pagination for high-volume collections, batched response enrichment, centralized permission filtering, PostgreSQL-native full-text and trigram search, and database constraints for important permission invariants.

The highest-value improvements are:

1. Remove transitional authorization fallbacks after verifying production ACL coverage.
2. Add or correct database constraints for case-insensitive identifiers and concurrent idempotent writes.
3. Eliminate confirmed N+1 behavior in deletion-request and join-request collections.
4. Validate a small set of reverse foreign-key and relationship-table indexes.
5. Make pagination and filtered collection results deterministic.
6. Reduce duplicated query construction without introducing an overly generic repository abstraction.
7. Establish production query measurement before adding indexes.

## Existing strengths

### Dataset and sample pagination

Dataset and sample collections use MFID keyset pagination ordered by `unique_id DESC`. Later cursor pages avoid a redundant count, while offset pagination remains available for compatibility. This is appropriate for the largest collections and avoids increasingly expensive deep offsets.

Relevant implementations:

- `app/api/routes/datasets.py:list_datasets`
- `app/api/routes/samples.py:list_samples`

The current cursor assumes MFIDs retain their documented time-sortable ordering. That invariant should remain covered by tests if the MFID implementation changes.

### Batched collection enrichment

Dataset and sample collections batch-load owners, project references, instrument references, links, and associated resources. They do not issue one query for every response object. This is consistent with SQLAlchemy guidance for avoiding N+1 relationship loading.

The use of explicit batch queries is preferable here to joined eager loading of multiple collections, which could multiply primary result rows. SQLAlchemy generally recommends select-in loading or equivalent batched loading for collections.

### Centralized permission filtering

`permission_filter()` builds a correlated `EXISTS` predicate rather than joining `ResourceACL` into the primary collection query. This has several advantages:

- Authorization is enforced by the database query.
- Multiple matching ACL rows do not duplicate resources.
- Counts do not require `DISTINCT` solely because of authorization joins.
- The same role semantics are reused across resource collections.

The current ACL indexes support both primary access paths:

- The `ResourceACL` primary key `(mfid, accessgroup_id)` supports fetching ACL entries for a known resource.
- `ix_resourceacl_group_mfid_permission(accessgroup_id, mfid, permission)` supports group-scoped collection filtering.

### Database-enforced permission invariants

Partial unique indexes enforce several important business rules:

- One owner ACL per resource.
- One owner membership per access group.
- One active deletion request per resource.

These are stronger than application-only checks because they also hold under concurrent requests.

### Search indexes

Search uses native PostgreSQL features:

- `pg_trgm` GIN indexes for fuzzy text fields.
- A GIN functional full-text index for scientific metadata.

The scientific metadata query expression must remain structurally identical to the indexed expression for PostgreSQL to use the functional index.

### Concurrency handling for deletion reviews

Deletion request review uses `SELECT FOR UPDATE` on the request and resource. This is an appropriate use of row locks for a state transition that must not be reviewed concurrently.

### Centralized resource detail dispatch

Generic resource detail behavior is centralized in `app/services/resource_details.py`. This reduces duplication between generic resource routes and typed routes while preserving resource-specific response construction.

## Priority 1: security and authorization consistency

### Remove transitional authorization fallbacks

Two pre-migration compatibility paths remain in `app/utils/auth_utils.py`:

- `has_platform_admin_access()` accepts membership in the legacy `admin` access group in addition to `platform_role`.
- `require_permission()` can accept a legacy project or instrument group when a resource has no ACL rows.

These paths were useful while application deployment and the permission backfill could occur in either order. Production has now completed the permission migration, so they represent parallel authorization systems.

Risks include:

- Unexpected access through stale legacy groups.
- Different authorization results depending on whether a resource happens to have any ACL rows.
- More difficult auditing because effective authority has multiple sources.

Before removal, run a read-only audit confirming:

- Every supported project and instrument has the expected ACL representation.
- Every platform administrator has the correct `platform_role`.
- No active integration depends on the legacy `admin` group.
- Ownerless resources that intentionally remain ownerless still have the intended non-owner ACL entries.

After that audit, remove the group-based platform bypass and the `legacy_group` permission fallback.

### Authentication cache revocation delay

Authentication results are cached per process. Invalidation affects only the current worker, while other workers or pods retain their entry until the TTL expires.

This means token revocation and role changes can take up to the cache TTL to become effective everywhere. The current behavior should either be:

- Documented as the credential and permission revocation SLA.
- Replaced later by shared or versioned invalidation.
- Given a shorter TTL if immediate revocation is operationally important.

### API token storage

Status: deferred future security expansion. It is not part of the current query and authorization cleanup.

`ApiToken.token` is the database primary key and stores the bearer token itself. A database disclosure would therefore expose immediately usable credentials.

A future design should store only a cryptographic digest of each high-entropy token and query by the digest of the presented token. The raw token would be returned exactly once when it is created or rotated. A lost token would be replaced through rotation rather than recovered.

This changes credential-retrieval semantics even though existing bearer tokens can continue authenticating throughout a staged migration. In particular:

- `GET /account/apikey` could return key metadata and a masked fingerprint, but not the existing secret.
- `POST /users/{user_id}/apikey` should issue or rotate a credential rather than return an administrator-readable existing secret.
- Browser-based API-key retrieval would need an explicit issue-once or rotation flow.
- Service-account creation and rotation already follow the intended return-once model.

A safe migration would add a digest column, backfill it from existing raw tokens, authenticate against digests, migrate clients away from secret-retrieval behavior, stop storing new raw tokens, and only then erase the legacy raw values. Existing keys would continue working after their raw values are erased, but could no longer be displayed. Reversible encryption is not the preferred substitute because disclosure of both the database and encryption key would expose every credential.

### Fail-closed behavior

Unknown database role values currently fail closed to viewer or no platform authority. That is the correct security direction. Database constraints should nevertheless prevent invalid role strings from entering the database in the first place.

## Priority 2: database constraints and concurrency

### Case-insensitive identifiers

The API frequently performs case-insensitive equality predicates such as:

```python
func.lower(User.email) == normalized_email
func.lower(User.username) == normalized_username
func.lower(Project.project_id) == normalized_project_id
func.lower(Instrument.instrument_id) == normalized_instrument_id
```

A standard B-tree index on the original column generally cannot service `lower(column)` equality efficiently. More importantly, a normal unique constraint remains case-sensitive, so application collision checks can disagree with database uniqueness.

Application checks also cannot prevent concurrent inserts where both transactions observe no conflict before writing.

For canonical identifiers, choose one database-backed policy:

1. Normalize to lowercase on every write and store only the canonical representation, using ordinary unique indexes.
2. Preserve the original representation and add a unique expression index on `lower(column)`.
3. Use PostgreSQL `citext` when the entire column is genuinely case-insensitive and the extension is acceptable.

Recommended domain choices:

- Normalize project slugs, instrument IDs, and usernames when their external contracts permit it.
- Add case-insensitive email uniqueness only after confirming that email uniquely identifies a user in the product contract.
- Keep instrument names as display values. Do not make display names canonical identifiers solely to optimize a legacy lookup.

The current bulk user-resolution implementation should also use the same normalization semantics as single-user resolution. Mixed-case stored usernames should not behave differently depending on which resolver is used.

### Database-backed idempotency

Several operations use a select-then-insert pattern:

1. Query for an existing row.
2. Insert when no row is found.

This can race under concurrent requests. Relevant areas include:

- Pending access-group join requests.
- Keyword creation and dataset-keyword links.
- File deduplication.
- Access-group creation.
- ACL provisioning helpers.

Where idempotency is intended, use:

- A database unique or partial unique constraint expressing the invariant.
- PostgreSQL `INSERT ... ON CONFLICT DO NOTHING` or `DO UPDATE` through SQLAlchemy's native PostgreSQL `Insert` API.
- Consistent translation of true conflicts into HTTP 409 where the operation is not idempotent.

Pending access-group join requests now have a partial unique index on `(accessgroup_id, requester_id)` where `status = 'pending'`. The index is created concurrently so its production deployment does not block ordinary table reads or writes. The creation route retains its friendly preflight check and translates a constraint race into HTTP 409.

### Join-request review locking

Join-request review now locks the request with `FOR UPDATE` before checking its pending status. Membership creation and the request transition remain in the same transaction, while membership primary keys prevent duplicate links.

### Integrity error handling

Database uniqueness is the final authority under concurrency. Creation routes should catch the relevant `IntegrityError`, roll back the transaction, and map known constraint violations to stable API errors. Preflight queries remain useful for friendly messages but are not sufficient for correctness.

## Priority 3: confirmed query inefficiencies

### Deletion-request collection N+1 behavior

`list_deletion_requests()` now joins resource type and dataset or sample project information into the page query. Response assembly performs no database access, and the collection uses one count query plus one page query.

### Join-request collection N+1 behavior

`list_join_requests()` now joins `AccessGroup` and `Project` into the page query. Response assembly performs no per-item queries.

### File detail lazy-load risk

File detail and update responses now use the `Dataset.unique_id` already returned by `_get_af_with_read_access()`. They no longer access the unloaded `AssociatedFile.dataset` relationship.

### Sample detail compatibility payload

Sample detail historically embeds associated datasets. This can become an unbounded response and requires additional permission-filtered dataset queries plus reference resolution.

The existing singleton response remains compatible and embeds datasets by default. The field is deprecated in OpenAPI, and clients can pass `include_datasets=false` to avoid the query. New clients should use `include_links=true` for lightweight relationship discovery and `GET /datasets?sample_mfid={sample_mfid}` for complete paginated dataset responses.

## Priority 4: index candidates

PostgreSQL does not automatically create indexes on the referencing side of foreign keys. Relationship and deletion queries must therefore be compared with the actual index order.

### Strong candidates

| Table | Candidate index or constraint | Existing access pattern |
|---|---|---|
| `useraccessgrouplink` | `(accessgroup_id, user_id)` | List project or access-group members and resolve ownership by group |
| `datasetsamplelink` | `(dataset_id, sample_id)` | Expand samples from dataset pages and traverse the graph from datasets |
| `samplelink` | `(parent_sample_id, sample_id)` | Traverse children from a parent sample |
| `datasetkeywordlink` | `(dataset_id, keyword_id)` | List or delete keyword links for one dataset |
| `associatedfile` | `(dataset_id, sha256_hash)` | List files for a dataset and perform upload deduplication |
| `thumbnail` | `(dataset_id)` | Load and delete thumbnails for a dataset |
| `accessgroupjoinrequest` | `(accessgroup_id, status, id DESC)` | List recent requests scoped to a group and status |

### Associated-file uniqueness

The API documents file creation as idempotent for the same dataset and SHA-256 hash. If that is the intended invariant, consider a unique constraint on `(dataset_id, sha256_hash)` after auditing and resolving existing duplicates.

That composite index supports dataset-scoped file operations because `dataset_id` is the leading column. It does not efficiently support the global `GET /files?sha256_hash=...` filter by itself. If that global lookup is operationally important and frequent, a separate index on `sha256_hash` may be justified.

### Composite index order

Composite B-tree indexes should put the common equality scope first, followed by range or ordering columns. Examples include:

- `(accessgroup_id, status, id DESC)` for scoped join requests.
- `(status, request_time DESC, id DESC)` for global deletion-request review queues if that becomes a measured hot query.
- `(user_unique_id, token_created DESC)` if users can have several token records and token history is queried by recency.

These should be selected from actual query frequency and plans rather than created speculatively.

### Indexes to audit for removal

Indexes have write, vacuum, memory, and storage costs. Before adding indexes, inspect `pg_indexes`, `pg_stat_user_indexes`, index sizes, and scan counts for:

- Boolean indexes on `ResourceACL.read` and `ResourceACL.write` after those legacy columns are retired.
- The legacy `Dataset.public` index when ACL visibility is the only authority.
- Explicit indexes on columns already fully covered by primary-key or unique indexes.
- Legacy project, instrument, and access-group indexes no longer used by current query paths.

Do not remove an apparently unused uniqueness index if it enforces a business invariant. Usage statistics alone do not reveal constraint value.

## Priority 5: query correctness and deterministic behavior

### Keyword filtering

`generate_kw_query()` currently has two correctness issues:

- It returns a Python list when no keywords match, although callers expect a SQL statement.
- Joining multiple matching keywords can return the same dataset more than once, affecting both results and counts.

Return a query with a false predicate for no matches, and use a correlated `EXISTS` for matching dataset-keyword links. This avoids duplicate outer rows and keeps counts correct without `DISTINCT`.

If substring keyword search becomes frequent and the keyword table grows materially, validate whether a trigram index on `Keyword.keyword` is warranted.

### Deterministic file pagination

`GET /files` applies `OFFSET` and `LIMIT` without an `ORDER BY`. PostgreSQL does not guarantee row order without an explicit ordering clause, so repeated pages can overlap or omit rows as plans and data change.

Add a stable unique ordering, such as `AssociatedFile.id DESC` or `mfid DESC`, and use the same ordering for any future cursor support.

### Stable search ordering

Fuzzy-search routes order by rank but do not always add a unique tie-breaker. Equal scores can therefore be returned in an unstable order.

Add `unique_id` as the final ordering key after relevance rank.

### Precise cursor detection

Dataset and sample collections currently return `next_cursor` whenever a page contains exactly `limit` rows. The following page may be empty.

Fetch `limit + 1`, use the extra row only to detect whether another page exists, and return at most `limit` items. This prevents one unnecessary request at the end of an exactly full result set.

### Dynamic query parameters

Dataset and sample collections inspect `request.query_params` and apply values to matching SQLModel attributes. Values are bound parameters and columns are selected through model attributes, so this is not a direct SQL injection path. It nevertheless has drawbacks:

- Unknown parameters are silently ignored.
- Typographical errors look like successful requests.
- Dynamic values remain strings instead of receiving field-specific validation.
- The OpenAPI contract does not fully describe the accepted filters.
- Dataset and sample filtering behavior can drift.

Prefer explicit typed parameters. FastAPI's current query-parameter-model feature supports Pydantic models with `extra="forbid"`, but the repository is pinned to FastAPI 0.112.2. Evaluate the native mechanism as part of a controlled FastAPI upgrade rather than implementing a custom parser.

Until then, use explicit parameters or a shared allowlist and field parser for the two collection routes.

### Trigram threshold binding

Search code interpolates the configured trigram threshold into `SET LOCAL`. The value comes from trusted application configuration, which limits exposure, but bound values are still preferable.

Where PostgreSQL syntax prevents a normal bind in `SET`, use `set_config('pg_trgm.word_similarity_threshold', :value, true)` with a bound and range-validated value.

## Priority 6: schema consistency

### Multiple sources of truth

Several compatibility columns represent the same concept:

- `ResourceACL.permission` versus legacy `read` and `write`.
- Public ACL membership versus `Dataset.public` and `Resource.public`.
- `project_mfid` versus legacy project IDs.
- `instrument_mfid`, integer instrument ID, and stored instrument name.
- `UserAccessGroupLink` versus `UserProjectLink`.
- `owner_user_id` versus `owner_orcid`.

Compatibility fields are reasonable at the API boundary, but they should not remain independent database authorities. Use one canonical field for authorization and relationships, derive compatibility response values from it, and audit all write paths until legacy storage can be removed.

### Timestamp types

Creation, modification, request, token, ingestion, and upload-expiration timestamps are commonly modeled as strings. Some database values are already returned as datetime objects, requiring defensive type handling.

Timezone-aware Python `datetime` values backed by PostgreSQL `TIMESTAMPTZ` would provide:

- Consistent timezone semantics.
- Typed FastAPI validation.
- Reliable range comparisons and ordering.
- Clearer index statistics and query plans.

This is a future schema migration and should not be mixed into immediate query-index work.

### Scientific metadata uniqueness

If each resource is intended to have at most one scientific metadata row, that relationship should be expressed by a unique constraint on `scientific_metadata_new.unique_id` and reflected consistently in SQLModel metadata and Alembic migrations.

### Upload-session referential consistency

`UploadSession.dataset_mfid` is not modeled as a foreign key, `owner_user_id` is a string despite its name, and `expires_at` is a string even though the database may return a datetime. These should be reconciled in a later schema-quality migration after confirming cleanup and expiration behavior.

### Reproducible schema definition

The historical schema is split across SQLModel declarations, data SQL scripts, and additive Alembic migrations. Before future major migrations, verify that a fresh database can be brought to the current schema reproducibly and that model metadata reflects production constraints and indexes.

## Priority 7: query architecture and code reuse

### Appropriate reuse boundary

The code should avoid both duplicated route queries and an overly generic repository that hides SQL behavior.

A useful structure is:

1. A resource-specific base statement builder that includes authorization, deletion visibility, and canonical relationship joins.
2. A shared filter applicator for common creation/modification time, visibility, and access-selector filters.
3. Resource-specific filter functions for dataset or sample fields.
4. Batch enrichment helpers for links, owners, and references.
5. Response assembly that performs no database access.

This keeps SQL visible and testable while preventing dataset, sample, search, and relationship routes from independently reimplementing authorization and reference resolution.

### Current duplication candidates

- Dataset and sample `apply_filters()` blocks repeat most collection semantics.
- `_dataset_base_query()` and `_sample_base_query()` overlap with their corresponding list-route query construction.
- Project collection logic is repeated in project and user routes.
- Instrument response construction is repeated between typed routes and resource-detail services.
- Singleton link resolution and batched collection link resolution implement the same relationship semantics separately.

The solution should be small statement-building functions, not a framework-wide generic CRUD layer.

### Guarding against accidental lazy loads

For service and response-assembly code, consider using SQLAlchemy `raiseload` in focused tests or selected queries. That causes an unexpected relationship access to fail rather than silently issue a query. It is useful for enforcing the design rule that collection serialization must not touch the database per item.

## Connection management

### Existing configuration

The application correctly creates one cached SQLAlchemy engine and configures:

- `pool_pre_ping=True`
- `pool_timeout`
- `pool_recycle`
- A bounded `QueuePool`

### Connection budget

The defaults permit up to 30 simultaneous connections per process:

```text
pool_size 15 + max_overflow 15 = 30
```

The actual worst-case database demand is approximately:

```text
(pool_size + max_overflow) * workers_per_pod * maximum_pods
```

This must fit within the Cloud SQL connection budget while leaving capacity for migrations, administration, monitoring, and other services.

Pool settings should therefore be derived from the deployment topology and database limit rather than tuned for one process in isolation.

### Transaction and statement timeouts

Consider applying appropriate connection-level defaults for API traffic:

- `statement_timeout` to stop unexpectedly expensive requests.
- `lock_timeout` to prevent API requests from waiting indefinitely on locks.
- `idle_in_transaction_session_timeout` as protection against abandoned transactions.

Timeouts should be configured specifically for the API role or connection setup, not indiscriminately at a cluster level. Migrations and administrative jobs may require different limits.

## Measurement plan

### Production query inventory

Enable or inspect `pg_stat_statements` and rank normalized statements by:

- Total execution time.
- Mean and maximum execution time.
- Call count.
- Rows returned or affected.
- Shared blocks read versus cache hits.
- Temporary blocks written.
- Planning time when planning statistics are enabled.

This identifies both individually slow queries and inexpensive queries that consume significant total load through high frequency.

### Query plans to capture

Capture `EXPLAIN (ANALYZE, BUFFERS)` for representative requests using realistic result sizes and selective as well as non-selective parameters:

1. Default dataset first page.
2. Later dataset cursor page.
3. Dataset filtered by project and instrument.
4. Dataset with user and project access selectors.
5. Dataset with links and metadata.
6. Equivalent sample queries.
7. Project member listing.
8. Dataset file listing.
9. File lookup by SHA-256 hash.
10. Upload deduplication lookup.
11. Deletion-request list for a platform administrator and a scoped user.
12. Join-request list scoped to one project and to all projects led by a user.
13. Dataset, sample, user, project, and instrument fuzzy searches.
14. Scientific metadata search.

For write statements, run `EXPLAIN ANALYZE` inside an explicit transaction and roll it back when testing against a safe environment.

### Index verification

For every candidate index, record:

- The query it is intended to support.
- The before and after execution plan.
- Execution time and buffer changes.
- Index size.
- Expected write overhead.
- Whether an existing index becomes redundant.

Avoid forcing index usage or disabling sequential scans merely to make an index appear useful. Small tables should often use sequential scans.

### Statistics and maintenance

Confirm autovacuum and auto-analyze behavior for the largest and highest-churn tables, especially:

- `dataset`
- `sample`
- `idtype`
- `resourceacl`
- Relationship link tables
- `associatedfile`
- Ingestion and upload-session tables

If correlated filters produce inaccurate estimates, evaluate PostgreSQL extended statistics only for column groups proven to be strongly correlated. Do not create extended statistics broadly.

## Suggested implementation sequence

### Phase 1: measure and verify

- Inspect `pg_stat_statements` and index usage statistics.
- Capture representative execution plans.
- Audit production ACL coverage and legacy authorization dependencies.
- Audit duplicate files and duplicate pending join requests.
- Inventory actual production indexes rather than assuming model metadata matches the database.

### Phase 2: correctness and security

- Remove validated legacy authorization fallbacks.
- Add a partial unique constraint for pending join requests.
- Lock join-request rows during review.
- Correct keyword-query duplicate and empty-result behavior.
- Add deterministic ordering to file collections and search ties.
- Standardize case-insensitive canonical identifier rules.

### Phase 3: query-count reductions

- Batch deletion-request project resolution.
- Join or batch join-request project resolution.
- Remove accidental file serializer lazy loads.
- Add query-count tests for representative collection endpoints.

### Phase 4: measured indexes

- Add only the reverse and composite indexes supported by measured plans.
- Use concurrent index creation where appropriate for production-sized tables.
- Re-run plans and compare buffer usage.
- Remove only indexes shown to be redundant and not needed for constraints.

### Phase 5: code structure

- Extract narrowly scoped shared collection-filter and base-statement builders.
- Keep response construction resource-specific.
- Add tests that reject accidental N+1 behavior and authorization-filter drift.

### Phase 6: later schema hygiene

- Convert string timestamps to timezone-aware timestamp types.
- Retire legacy public, read/write, project, instrument, owner, and membership storage after client and data audits.
- Reconcile upload-session types and foreign keys.
- Establish a reproducible current-schema baseline.

API token digest storage is intentionally outside this sequence. Revisit it as a dedicated security migration after credential-issuance and retrieval contracts have been agreed with client owners.

## Testing recommendations

Tests should cover both correctness and query shape:

- Unauthorized rows never appear in data or counts.
- Multiple matching ACL entries do not duplicate resources.
- Multiple matching keywords do not duplicate datasets.
- Cursor and offset pages have stable ordering and no overlap.
- Page serialization performs a bounded number of queries independent of page size.
- Concurrent duplicate join requests result in one pending row.
- Concurrent idempotent file creation results in one file record.
- Role or token revocation behavior matches the documented cache SLA.
- Case variants of canonical identifiers cannot create ambiguous rows.
- Public visibility is derived from the authoritative ACL representation.

Query-count tests should assert an upper bound rather than an exact count when exact counts would make harmless query restructuring difficult. They should still prove that query count does not grow linearly with page size.

## Official references

- PostgreSQL constraints and foreign-key indexing: <https://www.postgresql.org/docs/current/ddl-constraints.html>
- PostgreSQL expression indexes: <https://www.postgresql.org/docs/16/indexes-expressional.html>
- PostgreSQL multicolumn indexes: <https://www.postgresql.org/docs/17/indexes-multicolumn.html>
- PostgreSQL `EXPLAIN`: <https://www.postgresql.org/docs/current/using-explain.html>
- PostgreSQL planner statistics: <https://www.postgresql.org/docs/current/planner-stats.html>
- PostgreSQL `pg_stat_statements`: <https://www.postgresql.org/docs/current/pgstatstatements.html>
- PostgreSQL row locking: <https://www.postgresql.org/docs/17/explicit-locking.html>
- SQLAlchemy relationship loading: <https://docs.sqlalchemy.org/en/20/orm/queryguide/relationships.html>
- SQLAlchemy PostgreSQL `ON CONFLICT`: <https://docs.sqlalchemy.org/en/20/dialects/postgresql.html>
- SQLAlchemy connection pooling: <https://docs.sqlalchemy.org/en/20/core/pooling.html>
- FastAPI query parameter models: <https://fastapi.tiangolo.com/tutorial/query-param-models/>
- Cloud SQL connection management: <https://docs.cloud.google.com/sql/docs/postgres/manage-connections>
