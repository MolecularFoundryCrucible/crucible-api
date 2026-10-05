# Hub API requirements implementation review

Archived status: Superseded by the active [Hub API handoff review](../hub_api_handoff_review.md). The web authentication exchange has since shipped; see [Hub authentication overview](../hub_auth_overview.md). Revalidate every item against the implementation before reusing it.

Date: 2026-09-16

## Purpose

This document evaluates the API work proposed by the Hub documentation and records a staged implementation direction for `crucible-api`. It is not an accepted contract or a commitment to implement every proposal.

The review covers:

- `crucible-hub/docs/crucible-api-handoff.md`
- `crucible-hub/docs/administration-workspace.md`
- `crucible-hub/docs/api-integration.md`
- `crucible-hub/docs/roadmap.md`
- `crucible-hub/contracts/crucible-api.json`
- `crucible-hub/contracts/crucible-api.lock.json`

When an implementation changes a Hub-consumed contract, update the corresponding Hub documentation, pinned OpenAPI artifact, contract lock, generated client, adapters, and tests in a separately authorized Hub change. Do not modify the Hub repository implicitly as part of API work.

## Verified contract state

The Hub contract is pinned to API revision `33c129ebf5e40d390f2fa7693527e762adfd7121` with SHA-256 `3dc6c38515a95cf46cad0fd1c3c45bf26c30e3c05811faf5027992c4d5f13094`.

The pinned artifact is byte-for-byte identical to the checked-in OpenAPI artifacts on the current API `main` and `staging` branches. No OpenAPI refresh is currently required.

The following contracts are already implemented and should not be rebuilt:

- Owner-affiliation filters and preferred exact `owner_id` filters.
- Cursor pagination for samples, datasets, projects, and instruments.
- Sample and dataset sorting, time filters, null filters, totals, facets, facet ordering, and anchored navigation.
- API-owned UTC lifecycle timestamps and preservation of the producer-supplied dataset `timestamp` field.
- Service-account directory, creation, detail, platform-role update, key rotation, Contributor role, create-on-behalf behavior, and service-account-filtered public user search.
- Dataset instrument assignment and canonical project and instrument identifiers.

The Hub text saying that the Contributor role mutation still requires a staging recheck is stale. Staging and production completed revision `7a1c4e8d2f90`, the enum contains `none`, `contributor`, `support`, and `admin`, and the main API completed a healthy rolling deployment.

## Immediate correctness and authorization fixes

### Project creation authority

`POST /projects` currently lets any authenticated caller select any registered user as project lead. Ordinary callers should be restricted to creating a project led by themselves. A human platform administrator may create a project for another registered user.

Service accounts may create their own project when policy permits, but a non-administrator service account must not appoint an unrelated lead.

This change needs tests for a human creating for self, a service account creating for self, an ordinary caller attempting to appoint another user, a human platform administrator appointing another user, and an administrator service account attempting the same operation.

### Human-only identity administration

Service-account administration is not consistently restricted to human platform administrators. The account capability currently reports `can_manage_service_accounts=true` for any platform administrator, including a service account, while individual routes apply different restrictions.

Add one human-platform-administrator guard and apply it consistently to identity and platform-policy operations. Before enforcement, audit and migrate any remaining administrator service accounts whose current workflows depend on those routes.

Resource-level authority and exceptional administrator-service-account behavior must be treated separately from identity administration. Do not globally change platform-administrator semantics without reviewing every protected operation.

## Bounded contract-quality delivery

The following work is useful, independently testable, and mostly backward compatible:

1. Type the instrument status mutation parameter and `InstrumentResponse.status` with the existing `InstrumentStatus` enum.
2. Add typed response models for file-download capabilities, link removal, unpublishing, and other mutation operations that currently return untyped dictionaries.
3. Restrict the resource-access `kind` path parameter to supported writable principal kinds.
4. Add a writable access-permission enum that excludes `owner`, because owner changes require the ownership-transfer operation.
5. Document `ReassignProjectRequest.project_id` as the mutable project slug rather than an MFID.
6. Publish actual permission requirements and documented 400, 403, 404, 409, and 422 outcomes for thumbnails, project membership, instrument lifecycle, instrument registration, instrument operators, assignment, ownership transfer, and resource access.
7. Add a hard maximum of 100 deduplicated MFIDs to bulk first-thumbnail requests and represent that limit in OpenAPI.
8. Accept a validated `X-Correlation-ID`, include it in request logs, and echo the accepted or generated value in responses. Bound its size and character set, and never derive authorization or identity from it.

Shared error schemas may document existing `detail` responses, but this delivery should not attempt to redesign every API error at once. Typed domain conflict bodies should be introduced with the workflows that need them.

## Project and instrument collection parity

Add explicit lifecycle sorting and time bounds to project and instrument collections while preserving omitted-parameter behavior for existing clients.

Projects should retain their current project-ID ascending order when no sort is supplied. Explicit sorting should support `created`, `updated`, and `name`, with a deterministic MFID tie-breaker and null values last.

Instruments should retain their current creation-time descending order when no sort is supplied. Explicit sorting should support `created`, `updated`, and `name`, with the same deterministic cursor rules.

Both collections should accept `direction`, `creation_time_gte`, `creation_time_lte`, `modification_time_gte`, and `modification_time_lte`. Cursor validation must bind the authenticated principal, resource type, sort, direction, and complete result filter context.

## Production foundation

### Web authentication exchange

The current browser-oriented authentication flow is not suitable for the Hub. It uses a fixed nonce, does not establish validated OAuth state, and ultimately makes a long-lived Crucible API key available to a browser flow.

The Hub should own an opaque server-side session. If the API remains the ORCID OAuth client, implement a short-lived, single-use authorization code delivered only to an allowlisted Hub callback and consumed through a server-to-server exchange.

The exchange must define:

- Redirect allowlisting.
- State and nonce creation and validation.
- Client authentication and PKCE where applicable.
- Hashed one-time code storage.
- Atomic code consumption.
- Short expiry.
- Replay rejection.
- Session and credential revocation.
- Logout behavior.
- Credential rotation.
- Multi-replica behavior.

The Hub must never expose the Crucible API key, ORCID token, exchange code after consumption, or reusable platform credential to browser JavaScript.

### Creation idempotency

Add database-backed `Idempotency-Key` support to `POST /samples` and `POST /datasets` before enabling production Hub creation.

Scope each idempotency record by authenticated caller and operation. Store the key hash, canonical request fingerprint, processing state, resulting resource MFID, creation time, and expiry.

Required behavior:

- A new key and payload perform creation.
- The same key and payload after success return the authoritative created resource.
- The same key with a different payload returns `409`.
- A concurrent request with the same key cannot create a duplicate.
- Expiry and cleanup are documented.

Do not use a caller-assigned MFID as a substitute for idempotency.

### Project target discovery

`accessible_to_user` identifies every project readable by the selected user, including projects where the user is only a viewer. It therefore does not define a valid project picker for sample or dataset creation.

Add a bounded project selector that can require the selected user's effective `CREATE` permission, or extend the project collection with an explicit required-permission filter. The API must evaluate the intended owner's standing, not only the service account's platform-wide authority.

Do not overload `accessible_to_user` to mean ownership or creation eligibility.

### Browser upload

Use the existing exact-object GCS resumable-upload operation as the preferred browser-upload foundation. Do not expose the multipart operation that returns a broad storage access token.

Before production use, verify and document:

- Required GCS CORS behavior.
- Object-name reservation and collision handling.
- Retry and resume behavior.
- Checksum verification.
- Expiry.
- Abort and cleanup.
- Concurrent completion.
- Idempotent completion after a committed response is lost.

Implement downscoped multipart credentials only if parallel browser upload is a demonstrated requirement that resumable upload cannot satisfy.

## Administration MVP

The broad Administration proposal is directionally sound, but it should be delivered as separate domain workflows rather than one generic administration system.

### Join requests

Join-request hardening is the best first Administration workflow because the underlying submission, scoped listing, singleton read, and review operations already exist.

Add:

- Typed request-status and review-decision enums.
- A stable canonical project reference containing MFID, current project ID, and title.
- Resolved public-safe requester and nullable reviewer references.
- Canonical `project_mfid` filtering.
- Exact `can_review`, `can_cancel`, or typed `available_actions` guidance.
- The membership role approval will grant.
- Optional reviewer role selection constrained by server-provided allowed roles.
- Requester cancellation when policy permits.
- A version or equivalent stale-write precondition.
- An authoritative review result containing the reviewed request and resulting membership after approval.
- Typed duplicate-pending, requester-already-member, already-resolved, stale-version, deleted-project, and unavailable-requester conflicts.
- Opaque cursor pagination with deterministic newest-first ordering.

Add a canonical project-MFID submission operation and retain the current access-group-based path as a deprecated compatibility wrapper.

Do not expose internal access-group identifiers or require the Hub to interpret `group_name` as project identity.

### Account Administration capabilities

Expose navigation guidance only for implemented operations. Initial additions should be:

- `can_review_join_requests`
- `can_review_deletion_requests`
- `can_manage_service_accounts`
- `can_open_administration`

Every downstream operation must continue to authorize independently. Collection-level capability means the caller has at least one eligible scope; it does not imply authority over every item.

Do not add speculative processing, audit, or people-management capabilities until their corresponding operations exist.

### Administration summary

After join and deletion queues have typed filters and stable authorization, add one lightweight current-account summary such as `GET /account/administration-summary`.

Each category must distinguish unavailable from zero and state whether its count is exact, capped, or a lower bound. Include an API-generated UTC `as_of` or `updated_at` value. Do not calculate badges by requesting every first queue page.

### Deletion review

Current implementation policy already establishes that resource administrators and platform administrators may review deletion requests and that approval performs immediate hard deletion.

Strengthen the contract with:

- Typed request and decision states.
- Requester-readable singleton detail with exact `can_review` guidance.
- Canonical resource and project references.
- Public-safe requester and reviewer references.
- A bounded server-generated impact summary.
- A version or equivalent stale-write precondition.
- Typed conflict results.
- An authoritative reviewed request plus a typed tombstone after deletion.
- Opaque cursor pagination and deterministic ordering.

The current mismatch where a requester can see a request in the collection but cannot read its singleton detail should be corrected.

## Relationship management

Existing sample-parent, dataset-parent, and dataset-sample link operations provide much of the required behavior, but their public mutation contract remains incomplete.

Implement incrementally:

1. Reject self-links explicitly.
2. Preserve the current duplicate-resistant behavior and document it as idempotent when the requested relationship already exists.
3. Return typed canonical source and target MFIDs instead of exposing database link identifiers as the useful response identity.
4. Return an authoritative typed unlink result.
5. Publish relationship direction semantics and exact same-project and cross-project permission requirements.
6. Add relationship-specific capabilities if the Hub cannot safely derive presentation from existing resource capabilities.

Do not introduce cycle rejection until the domain explicitly decides that the corresponding relationship must form a directed acyclic graph. If cycles are prohibited, enforce the invariant server-side and add database-aware concurrency tests.

## Typed bounded resource graph

Replace the Hub dependency on the storage-named CTE endpoint with one canonical typed resource graph operation.

The initial operation should default to depth one and include:

- An explicit root MFID.
- Typed nodes with MFID, resource type, and display name.
- Typed edges with source, target, direction, and nullable relationship type.
- Authorization and deletion filtering for every returned node and edge.
- Deterministic ordering.
- Explicit node and edge limits.
- Truncation metadata.
- Typed 403, 404, and limit outcomes.

Do not expose unbounded recursive traversal initially. Add bounded depth or continuation only after query plans and production-size data demonstrate safe behavior.

## Account pins

Durable account pins are a coherent independent feature and can be implemented after the Administration MVP or when cross-device navigation becomes a priority.

Suggested operations:

- `GET /account/pins?resource_type=&cursor=&limit=`
- `PUT /account/pins/{resource_mfid}`
- `DELETE /account/pins/{resource_mfid}`

Pin and unpin must be idempotent. Ordering should use `pinned_at` descending with a deterministic MFID tie-breaker and opaque cursor pagination.

If access is lost, return only a removable unavailable tombstone containing the MFID, resource type when safe, and an unavailable reason. Do not retain or reveal the prior resource name, project, or scientific context without an explicit privacy decision.

Pins do not change resource authorization, ownership, lifecycle timestamps, or activity timestamps.

## Audit and activity

Do not synthesize activity from resource, project, or instrument `modification_time`. Audit and activity require durable event semantics.

Start with an immutable administrative event model written transactionally with the domain mutation. Each event should have a stable event ID, enum event type, canonical UTC occurrence time, public-safe actor when disclosable, canonical subject and scope references, safe previous and resulting state, and correlation reference.

Define retention, redaction after access loss, actor privacy, service-account representation, immutability, duplicate suppression, and visibility before deployment.

An account activity feed may later project the event families appropriate to the caller. Audit and user-facing activity should remain separate representations even if they share an underlying event store.

Polling a bounded first page is sufficient initially. Do not add server-sent events or WebSockets before durable replay, authorization, disconnect recovery, and operational limits exist.

## Service-account lifecycle and usage

Disablement, reactivation, revoke-only credential operations, expiration attention, assignments, and administrative audit all make sense, but active-job and revocation behavior must be defined first.

Do not synchronously update `last_used_at` on every authenticated request. Use throttled persistence, asynchronous events, or operational telemetry so high-frequency service accounts do not create a database write for every API call.

Human platform-role and lifecycle management should use dedicated typed operations. Do not extend generic user-profile PATCH routes into identity administration.

Account impersonation is not part of this plan.

## Processing operations

Do not build the broad processing Administration workspace on top of the current generic ingestion-request PATCH operation.

First define:

- Typed operation kinds and statuses.
- Request, start, update, and completion times.
- Safe structured failure codes and messages.
- Requester, dataset, file, project, and instrument references subject to authorization.
- Exact per-item actions.
- Explicit retry and cancel operations.
- Idempotency and concurrency behavior.
- Worker ownership and callback semantics.
- Cursor pagination and server-side filters.

The generic PATCH operation should be deprecated for interactive administrative actions after worker-compatible replacements exist.

## Collection summaries and saved views

Authorization-aware project sample and dataset counts and instrument dataset counts should use explicit opt-in summaries or bulk aggregate operations. They must define assigned versus shared project semantics and acquisition-instrument semantics.

Do not add counts to every collection row by default or issue per-row count queries.

Saved views should remain deferred until repeated cross-device demand exists. When implemented, persist a versioned validated filter and sort model rather than arbitrary URLs or a generic query language.

Month and year facets, unified sample-and-dataset collections, additional speculative facets, ETags on all resources, generic query languages, and generic expansion languages remain deferred.

## Explicit non-goals

- Do not update project or instrument modification timestamps to simulate child activity.
- Do not build a combined sample-and-dataset collection solely for an All tab.
- Do not group, sort, filter, facet, or paginate incomplete pages in the client and present them as complete collections.
- Do not add generic ETags across the entire API. Use version fields for concurrent review workflows and idempotency keys for creation first.
- Do not create a generic administration mutation form or database console.
- Do not make recently viewed resources API-owned unless cross-device history becomes a demonstrated requirement.
- Do not expose credentials, signed URLs, raw storage paths, scientific metadata, or unrelated private identity fields in audit, administration, activity, or error responses.
- Do not require a standing platform-administrator credential for normal Hub workflows.
- Do not use API endpoint failure as the Hub's navigation-discovery mechanism.
- Do not implement custom multipart credentials while exact-object resumable upload satisfies the demonstrated requirements.

## Policy decisions required before affected implementation

1. Which project roles may review join requests, and which roles may they grant?
2. May a requester cancel a pending join request, and can completed requests expire?
3. Is the current resource-admin deletion-review policy accepted for production?
4. What deletion impact information may be disclosed before approval?
5. What operational information may an instrument administrator see without independent dataset read access?
6. Which processing actions belong to resource editors, project administrators, instrument administrators, and platform operators?
7. What happens to active jobs when a service account is disabled or its key is revoked?
8. Which human platform roles are assignable, by whom, and with what propagation delay?
9. What audit retention and redaction rules apply after access is lost?
10. Which summary counts must be exact and which may be capped or lower bounds?
11. Which relationship types, if any, must be acyclic?

## Recommended implementation order

1. Fix project creation authority and human-only identity-administration inconsistencies.
2. Deliver the bounded contract-quality cleanup.
3. Add project and instrument sorting and lifecycle-time filter parity.
4. Implement the web login exchange and shared-session contract with the Hub.
5. Add sample and dataset creation idempotency.
6. Harden exact-object resumable upload for production browser use.
7. Strengthen join-request submission, listing, review, cancellation, references, capabilities, and concurrency.
8. Add implemented account Administration capabilities and the lightweight Administration summary.
9. Strengthen deletion review and deletion audit contracts.
10. Complete relationship mutation contracts and add the typed bounded graph operation.
11. Add durable account pins when prioritized.
12. Establish the durable audit event model before unified activity.
13. Add processing administration and identity lifecycle only after their policy and worker semantics are accepted.

## Delivery requirements

For every implemented stage:

- Preserve existing omitted-parameter and compatibility behavior unless an explicitly reviewed correctness or security fix requires a break.
- Add or update OpenAPI descriptions, schemas, responses, and examples.
- Regenerate and commit `openapi/crucible-api.json` and its digest.
- Add focused authorization, validation, pagination, concurrency, and compatibility tests.
- Add an `Unreleased` changelog entry for user-, operator-, or client-visible behavior.
- Add a matching Alembic revision for every `table=True` SQLModel change and keep one linear head.
- Make data audits read-only by default and require an explicit commit flag for writes.
- Do not run staging or production migrations without explicit user authorization.
- Update the Hub handoff and pinned contract only through a separately authorized Hub change.
