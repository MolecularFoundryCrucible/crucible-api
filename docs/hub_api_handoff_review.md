# Hub API handoff review

Status: Active decision tracker

Source: `/home/roncofaber/software/crucible-hub/docs/crucible-api-handoff.md`

## Synchronization requirement

Any decision or implementation that changes an item in this review must also update the source handoff document in the Hub repository. The API implementation, generated OpenAPI artifact, tests, changelog when required, this review, and the Hub handoff must describe the same supported contract before the Hub consumes it.

## Review principles

- Treat the handoff as design input, not mandatory scope.
- Prefer established HTTP, OAuth, FastAPI, and storage-provider mechanisms over custom protocols.
- Preserve API authorization as the authority for every operation.
- Add only behavior backed by a concrete Hub workflow.
- Define ordering, pagination, nullability, error, retry, and concurrency behavior where clients depend on them.
- Make new contracts additive and retain compatibility routes for a documented migration window when existing clients use them.
- Publish and verify the generated OpenAPI artifact before declaring a contract available.

## Recommended order

1. Secure browser upload contract (deferred)
2. Web authentication contract (deferred)
3. Creation idempotency (deferred)
4. GET query contract modernization
5. Focused OpenAPI contract cleanup
6. Join-request workflow
7. Project membership capability guidance
8. Conditional mutation safety
9. Update clearing semantics
10. Canonical resource graph contract
11. Relationship management contract
12. Dataset instrument reassignment
13. Remaining narrowly justified product features

## Decision tracker

### 1. Secure browser upload contract

Decision: Deferred on 2026-09-08. Do not expose real Hub uploads until this item is resumed and the contract is implemented.

The current multipart initiation route returns a short-lived token derived from the storage service account with broad `devstorage.read_write` scope. Its returned object prefix is informational and does not restrict the credential. The Hub must not use this route in production in its current form.

First evaluate the existing GCS resumable-upload route. It returns an object-specific resumable URI without exposing storage credentials and is the preferred native solution if browser performance and required CORS behavior are acceptable. Implement the deferred XML multipart and Credential Access Boundary design only if parallel browser upload is a demonstrated requirement.

Required decisions and verification:

- Confirm expected browser file sizes and acceptable upload performance.
- Verify resumable-upload CORS, retry, resume, checksum, expiry, abort, cleanup, and concurrent completion behavior.
- Ensure initiation reserves an exact destination object and completion is idempotent.
- If XML multipart is required, downscope client credentials to the exact object and keep completion and abort authority server-side.
- Do not expose the existing broad multipart token to the Hub.

### 2. Web authentication contract

Decision: Deferred on 2026-09-08 because the complete exchange and session integration requires a larger implementation effort. Production Hub authentication and mutation exposure remain blocked until this item is resumed and implemented.

Use an opaque server-side Hub session so API credentials and OAuth tokens are not exposed to browser JavaScript. Do not adapt the current browser API-key page into the Hub login mechanism.

Choose explicitly between:

- The Hub acts as the confidential OAuth client and maintains its own server-side OAuth session.
- The API remains the OAuth client and issues a short-lived, single-use authorization code to an allowlisted Hub redirect for authenticated server-to-server exchange.

If the API-owned exchange is selected, define client authentication, redirect allowlisting, state and PKCE handling where applicable, hashed single-use codes, atomic consumption, expiry, replay rejection, session revocation, logout, and credential rotation.

### 3. Creation idempotency

Decision: Deferred on 2026-09-08 while production Hub authentication and mutations remain blocked. Resume before enabling production Hub creation, or earlier if Nano or another current API client demonstrates uncertain create retries.

Creation idempotency protects against an ambiguous outcome rather than ordinary validation failures. A request can commit successfully while its response is lost. Retrying an auto-identified create can then create a duplicate, while refusing to retry forces the user to search for an inconclusive match.

Client-provided MFIDs provide partial protection because reusing one prevents a second identity, but the retry returns a collision instead of the original successful result. The API does not establish that the existing resource came from the same caller and payload, and the Hub intentionally lets the API assign canonical identity.

When resumed, prefer database-backed `Idempotency-Key` support for `POST /datasets` and `POST /samples`. Scope each record by authenticated caller and operation and persist the key, canonical request fingerprint, processing state, created resource MFID, creation time, and expiry. Required behavior:

- A new key and payload performs creation.
- The same key and payload after success returns the authoritative created resource.
- The same key with a different payload returns 409.
- A concurrent request for a key already in progress receives a documented conflict or retry response.
- A failure before commit permits a safe retry.
- A failure after resource commit recovers the created MFID rather than creating another resource.
- Validation failures do not reserve a key as a successful operation.

Use a database uniqueness constraint because in-memory state cannot coordinate API replicas or survive restarts. Define retention, cleanup, rate limits, request-fingerprint canonicalization, transaction boundaries, and background-work behavior. Store the created MFID rather than a serialized response, then reread through current authorization and serialization on replay so the client receives a current authoritative representation.

Alternatives considered:

- Client-generated MFIDs still need caller and payload matching to distinguish a replay from a collision.
- An idempotent `PUT /datasets/{mfid}` contract is HTTP-native but requires every client to generate valid MFIDs and introduces a second creation interface.
- Manual search after a timeout avoids automatic duplication but cannot identify a non-unique resource conclusively and provides poor recovery.

Do not extend the mechanism to other create operations until a client demonstrates the same retry requirement.

### 4. GET query contract modernization

Decision: Deployed to production on 2026-09-15. The supported contract is summarized in `docs/get_query_contract.md` and defined by OpenAPI.

Effective access and ownership are different relations. The existing `accessible_to_user` filters must not be overloaded to mean ownership, and the Hub must not filter already paginated results locally.

Add repeatable `affiliation` filtering to dataset, sample, instrument, and project collections and matching search endpoints, initially supporting `affiliation=owner`. Omitting the parameter returns every readable resource. Ownership means authoritative owner permission state for the authenticated caller, not a denormalized presentation field or general project membership. Preserve permission filtering so ownership does not imply visibility outside the caller's authorized view.

Replace raw model-column filtering with endpoint-specific typed query models after a reviewed FastAPI upgrade. Preserve legacy exact filters used by working clients as explicit deprecated parameters, preserve offset pagination, accept raw MFID cursors for the default order, and reject unknown parameters only after supported legacy inputs are declared.

Add `sort=created|updated|name` and `direction=asc|desc` to dataset and sample collections. Omitted sort preserves the current `unique_id DESC` order, while explicit `sort=created` uses authoritative resource creation time because callers can preassign MFIDs. Every ordering uses a stable unique-ID tie-breaker, missing values sort last, and new signed opaque cursors bind to the principal, resource kind, ordering, and result-shaping filters. Preserve legacy cursor precedence when cursor and offset are supplied together, validate time values as timezone-aware, and document mutable sorts as a live view under concurrent changes.

Add optional `anchor_mfid` placement to dataset and sample collections. An anchored request cannot include a cursor or explicitly supplied offset, starts with the anchor inclusively in the requested order, and applies authorization, deletion policy, scope, and filters before locating it. Missing or excluded anchors return 404. The response retains the existing shape and emits an ordinary `next_cursor` that can be followed without resending the anchor. Hub may obtain both local directions with two requests that share the same anchor, filters, and sort and use opposite directions.

Use `owner_id` as the preferred exact human-or-service-account filter on dataset, sample, and instrument collections and searches. Retain `owner_orcid` and `owner` as deprecated aliases, accept a preferred and legacy pair only when they match, and reject combining an exact owner with current-caller `affiliation`. Add `include_total` to all four collections: true requests an exact authorization-filtered count, false suppresses it, and omission preserves each endpoint's legacy count behavior. Total preference does not change cursor identity or row membership.

The implementation converts API-owned resource creation and modification values to nullable PostgreSQL `TIMESTAMPTZ` columns after a read-only audit. New resources receive API-generated UTC values and database defaults, while missing legacy values remain null. Unparseable non-null timestamps block migration, timezone-less legacy values are interpreted in `America/Los_Angeles`, missing modification times use creation times when available, explicitly offset values preserve their instants, and non-null API responses use canonical UTC `Z` formatting. The producer-supplied dataset `timestamp` field is outside this lifecycle policy. The migration requires pausing incompatible API versions. Cursor signing uses an identified key ring so new keys can be activated while old keys remain valid through a rotation window. Production query-plan evidence remains a deployment task, and no speculative index is included.

### 5. Focused OpenAPI contract cleanup

Decision: Implement as one bounded contract-quality change.

Include:

- Reference `InstrumentStatus` from the lifecycle mutation parameter and `InstrumentResponse.status`.
- Describe `ReassignProjectRequest.project_id` explicitly as the mutable project slug, not an MFID.
- Type supported resource-access principal path kinds.
- Add typed responses for access revocation and unpublishing.
- Publish actual 403, 404, and 409 responses for membership, lifecycle, ACL, reassignment, and ownership operations.
- Add thumbnail-name bounds.
- Enforce and document a hard maximum for bulk first-thumbnail requests rather than only logging large requests.
- Document thumbnail permissions, ordering, normalization, supported formats, limits, duplicate behavior, and error outcomes.
- Document instrument registration validation, collision behavior, owner resolution, and error outcomes.
- Document operator and ownership-transfer outcomes and post-mutation reread guidance.

### 6. Join-request workflow

Decision: Implement because the incomplete contract blocks the Hub workflow.

Add request and review status enums, canonical project identity, public-safe requester and reviewer summaries, an exact project filter for the current account's requests, stable ordering, documented pagination, explicit review capability guidance, the granted membership role, and typed 403, 404, and 409 outcomes. Review should return an authoritative result containing both the reviewed request and the resulting membership state.

Rename or replace the ambiguous response `group_name` field. It currently represents a project slug rather than a stable access-group identifier. Correct stale schema path descriptions at the same time.

### 7. Project membership capability guidance

Decision: Implement operation-specific guidance.

Do not use `can_transfer` as a proxy for member management. Editors and admins can manage some lower roles without ownership-transfer authority. Add guidance such as `can_manage_members` and `max_member_role`, derived from the same authorization logic enforced by membership mutations. Consider a separate `can_leave` value when owner self-removal is prohibited.

Document existing owner, platform-admin, delegated manager, and self-leave behavior with typed error responses. Separate member removal from self-leave only if doing so materially simplifies the Hub contract.

### 8. Conditional mutation safety

Decision: Implement incrementally for high-risk mutations.

Use a durable version value or strong ETag and require `If-Match` for operations where lost updates or stale confirmation are costly. Start with ownership transfer, project and instrument reassignment, ACL mutation, instrument lifecycle changes, and metadata replacement. Do not use a coarse timestamp as the only concurrency token.

Define the response and recovery behavior for missing and failed preconditions before expanding this to ordinary field edits.

### 9. Update clearing semantics

Decision: Define per field before exposing broad Hub editing.

For nullable fields, prefer omitted for unchanged and explicit `null` for clear. Required fields should reject `null` and invalid empty strings. Document intentional exceptions and ensure runtime behavior, Pydantic schemas, and OpenAPI agree. Do not introduce one global clearing rule without auditing each field.

### 10. Canonical resource graph contract

Decision: Implement if the Hub graph workflow remains in scope.

Add canonical resource-scoped graph routes with typed nodes and edges, explicit root identity, stable MFIDs, resource kinds, display names, relationship and direction semantics, bounded depth, deterministic ordering, result limits, truncation or continuation state, and permission-filtered traversal. Resolve open policy questions about crossing project boundaries and project-graph authorization first.

Retain current graph routes as deprecated wrappers around shared traversal and authorization logic for a documented compatibility window. Do not duplicate graph implementations.

### 11. Relationship management contract

Decision: Implement together with or after the canonical graph vocabulary.

Unify the fragmented link and unlink operations behind typed contracts. Define supported source and target combinations, edge direction, self-links, duplicates, cycles, missing targets, authorization, and conflict behavior. Return authoritative typed source, target, and relationship representations rather than message dictionaries or unrelated full collections.

Expose relationship-specific capability guidance only when it is derived from the same server authorization used by the mutation.

### 12. Dataset instrument reassignment

Decision: Implemented with `PUT /datasets/{dataset_mfid}/instrument`.

The operation resolves a stable instrument identifier, updates all stored instrument references and the derived contributor grant atomically, advances modification time, and leaves unrelated grants unchanged. Generic dataset PATCH cannot reassign or clear an instrument. Clearing remains unsupported. See `docs/dataset_instrument_assignment.md`.

### 13. Search and collection semantics

Decision: The affiliation filter, minimum dataset and sample sorting contract, and bounded dataset and sample facet endpoints are accepted as part of `docs/get_query_contract.md`. General group summaries and a broad aggregation query language remain deferred.

Dataset facets cover session, measurement, data format, owner, instrument, and project. Sample facets cover sample type, owner, and project. Stored values remain exact, every bucket carries a canonical display label, null receives its own selectable bucket, relation buckets carry canonical identifiers and resolved references, and counts apply the caller's authorization and supplied scope filters. Bucket results use bounded cursor pagination and support raw-value, case-insensitive label, or count ordering. Label and count ties use raw stored values, and null remains last. Continuations resend the same field, filters, sort, and direction. Month and year grouping remains deferred because the producer-supplied timestamp contract is not enforceable; a future creation-time facet must define UTC calendar semantics explicitly.

### 14. Home and activity summaries

Decision: Defer until the underlying product concepts are defined.

Pins and recently viewed resources require persistence, privacy, retention, and ordering decisions. Pending-work summaries should be built from authoritative workflow resources. Implement those resources first, measure the Hub's request and latency needs, and add a bounded composition endpoint only if it provides a demonstrated benefit.

Do not update a project's or instrument's `modification_time` when a child dataset or sample changes. That timestamp remains the lifecycle time of the resource itself. If latest related activity becomes a product requirement, add a separately named `last_activity_time` backed by defined durable events or a purpose-built aggregate.

Project sample and dataset counts and instrument dataset counts remain deferred. If required, expose them through an explicit opt-in and bulk authorization-aware aggregates. Project counting semantics must distinguish primary assignment from shared access before implementation.

### 15. Mutation response models

Decision: Improve incrementally, not through one universal response shape.

Each mutation should return a typed authoritative representation or a typed operation result sufficient for the client to reconcile state. Prioritize operations used by the Hub. Define download-link capabilities, expiry, MIME behavior, and content-disposition guidance separately because they are not ordinary resource mutations.

### 16. Canonical access principals and Resource Access

Decision: Improve write and manager contracts now; defer broader ACL visibility pending a privacy policy.

Use stable typed principal representations and operation-specific capabilities. Constrain writable principal kinds to supported user and project forms, remove owner from ordinary grant writes, paginate potentially unbounded grant lists, type mutation responses, and document identifier resolution and error outcomes.

Do not automatically expose full ACLs to non-managers. Grants may reveal identities and project associations. If the Hub needs a read-only view, define a redacted representation and explicit visibility rules. A caller-specific effective-access response is safer for ordinary detail views.

### 17. Instrument operator contract

Decision: Document and type existing behavior first.

Add operation-specific operator capability guidance and explicit 403, 404, and conflict outcomes. Keep exact service-account lookup unless a demonstrated operator workflow requires browsing. A bounded discovery endpoint should be added only with an identity-visibility policy.

### 18. Instrument lifecycle and registration

Decision: Include immediate schema and documentation cleanup; defer additional policy until specified.

Type the existing lifecycle values and document whether all transitions are currently allowed, including restoration from decommissioned. Add conditional mutation protection before relying on lifecycle state in concurrent administrative workflows. Publish the existing instrument ID validation, case-insensitive collision behavior, owner resolution, and actual error outcomes.

### 19. Project reassignment and ownership transfer

Decision: Preserve existing preview behavior and improve the contract.

Document source and target authorization, slug semantics, 400, 403, 404, and 409 outcomes, and the authoritative response. After confirmed ownership transfer, instruct clients to reread the resource because capabilities and visibility can change. Add conditional confirmation once resource versioning is available.

## Completion criteria for each accepted item

- The design decision and compatibility impact are recorded here.
- Runtime authorization and validation are covered by tests.
- Concurrency and replay behavior are tested where relevant.
- OpenAPI generation is deterministic and the checked-in artifact is refreshed.
- `CHANGELOG.md` is updated for externally observable changes.
- The Hub handoff source document is updated in the same workstream.
- The Hub consumes the feature only after the API contract is available in the intended environment.
