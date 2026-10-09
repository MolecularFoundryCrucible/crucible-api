# Secure multipart upload implementation proposal

## Status and scope

Design review updated 2026-09-28. The [stage 1 provider experiment](parallel_upload_stage1.md) passed with signed part URLs, including a real Firefox upload and legacy-credential isolation checks. The foundation and API/worker implementation are now available on the isolated feature branch; they are not deployed. See the [implemented HTTP contract and operator guide](multipart_upload_api.md). Introduce a separate multipart route family, preserve existing request and response schemas for legacy clients, and migrate clients in measured cohorts before retiring unrestricted token issuance. This supersedes the previous proposal to change the protocol behind the existing initiation route.

The review covers API contracts, client behavior, GCS authorization, object identity, integrity, database consistency, retries, cleanup, operational isolation, and rollout. Stage 1 created disposable experiment infrastructure and confirmed the locally installed Nano upload path. Production IAM, deployed client versions, large-file performance, and the remaining browser matrix still require validation. Application behavior and public schemas remain unchanged.

## Proposed change

Add a separate multipart upload API backed by the existing `UploadSession` and `AssociatedFile` tables. The API initiates GCS XML multipart uploads and signs individual part PUT requests. Clients send bytes directly to GCS in parallel. The API controls completion, verifies the completed object, and registers the existing associated-file response. Old clients continue using their current endpoints until a published retirement date.

The initial implementation adds no session, part, or provenance tables and does not replace ORCID authentication. Upload routes consume the shared authenticated principal and dataset permission checks. The server uses a dedicated storage identity whose credentials are never returned to clients.

Stages 1 and 2 are complete. The API and worker implementation is ready for local review and validation, followed by client adoption and a separately authorized staging pilot. The detailed contract and release gates below are the proposed implementation baseline.

## Compatibility commitment

| Existing behavior | During migration |
| --- | --- |
| Legacy multipart initiation request and response | Preserved while that route remains enabled |
| Existing resumable upload route | Preserved |
| Legacy completion request and file response | Preserved for legacy sessions |
| Existing `UploadSession` rows | New fields remain null or use compatible defaults |
| Existing file records and download identifiers | Preserved; no bulk object relocation |
| Existing client authentication | Continues through the shared authentication dependency |
| New session submitted to legacy completion | Rejected before expiry deletion or storage access |

Database expansion is compatible with the old insertion paths. New sessions must not be issued while unpatched API instances can process them: first deploy protocol guards everywhere, then enable the feature. Once new sessions exist, rollback must retain the guard and their completion/cleanup handlers. Disabling the legacy initiation route is an intentional client migration milestone, not an incidental consequence of the new schema.

## Implementation sequence and review units

| Phase | Deliverable | Exit condition |
| --- | --- | --- |
| 1. Provider experiment | Signed upload, completion, abort, browser, and isolation evidence | Completed; see [stage 1](parallel_upload_stage1.md) |
| 2. Foundation | Additive columns on the two existing tables, protocol guards, storage configuration interfaces, and a disabled-by-default feature flag | Local database upgrade and compatibility tests pass; old API models and behavior are preserved |
| 3. API and processing | Initiation, part authorization, resume/status, completion, abort, durable verification, and reconciliation | Authorization and concurrent/retried/crashed operations produce one file or a recoverable cleanup state |
| 4. Client integration | Nano implementation, browser integration contract, updated OpenAPI artifacts and examples | An updated client resumes interrupted uploads and downloads/ingests the resulting files |
| 5. Staging and pilot | Permanent staging storage, isolated credentials, large-file measurements, and client cohorts | Functional, isolation, integrity, performance, and cleanup gates pass |
| 6. Retirement | Legacy-caller tracking, restriction, cutoff, and session drain | No remaining supported client depends on unrestricted upload tokens |

Keep review units independently understandable. Foundation code can be reviewed before any new endpoint accepts uploads. Existing session rows can serve as the durable work list for the processing worker; provider calls occur outside database locks and a lease controls which worker owns an operation. Reuse the established worker deployment approach where appropriate without requiring a new application table just to enqueue work.

Schema changes should first be tested against a disposable local database. Applying migrations, changing deployed IAM, or deploying to shared staging/production is a separate operation requiring explicit authorization under this repository's instructions. The earlier authorization for the stage 1 experiment does not authorize those later operations.

## Working in parallel with the ORCID agent

Worktree isolation was established on 2026-09-28. Multipart work uses `/home/roncofaber/software/crucible-api-multipart` on `feat/secure-multipart-upload`. The ORCID work keeps `/home/roncofaber/software/crucible-api` on `feat/hub-login-design`. The multipart proposal, review, probe scripts, and result files were transferred with hash verification and a backup; the other agent's changes and Git index were preserved. The ownership and integration rules below remain coordination proposals until acknowledged by the other agent.

The multipart branch starts at the local reviewed `origin/staging` snapshot `f9fb3c7`. It does not include the unmerged ORCID feature commits. Use explicit commands in the multipart worktree for its edits, tests, and commits. Ignored environment files and credentials were not copied. Before implementation, refresh the integration base and configure an independent local test environment. A branch switch in the shared directory is not sufficient isolation. Codex supports separate worktrees for concurrent work; see the [official worktree guidance](https://learn.chatgpt.com/docs/environments/git-worktrees).

| Area | Proposed ownership and integration rule |
| --- | --- |
| ORCID routes, login/token utilities, and auth documentation | ORCID agent owns these; multipart consumes the existing principal/permission interface |
| New multipart route module, storage helper, worker, tests, and upload documentation | Multipart agent owns these in its separate worktree |
| `app/models/core_models.py` | Multipart changes only `UploadSession` and `AssociatedFile`; reconcile with the auth model additions at integration |
| `app/api/main.py` and `CHANGELOG.md` | Keep edits focused on route registration and the feature's own entry; resolve overlaps before merging |
| `migrations/versions/` | Separate revision files; agree on merge order and preserve one linear head |
| `openapi/crucible-api.json` and its checksum | Regenerate from the combined routes after integration; do not hand-merge generated JSON |
| Dependency and lock files | Reuse the tested installed libraries; coordinate any dependency changes rather than mixing independent lockfile rewrites |

Integration completed against `origin/main` commit `8e4d0ef`, which contains the reviewed ORCID workflow and rollout changes. The unapplied multipart foundation now follows auth revision `e92a6b1c730f`, and multipart processing remains the single head at `c83a7d014b29`. Applied auth revisions were left unchanged. Deployment and shared database migration remain separately authorized operations.

Separate worktrees isolate source files and Git indexes, not databases, cloud IAM, ports, or credentials. Use independent local test databases and worktree-local configuration. Auth currently reserves ports 3000, 7777, and 7778 according to its design notes; select a different available port for multipart development. Share the stage 1 bucket names and signer as explicit experiment resources, and do not alter the other agent's credentials, API processes, or staging database. Coordinate deployments separately from code development.

Each agent can now commit independently in its own worktree. Do not stage, stash, switch branches, or clean up files in the other agent's checkout. Shared Git objects and remote references still exist, so coordinate integration and pushes. Changes reach `main` through a reviewed branch/PR, never through a direct push.

Suggested handoff to the ORCID agent: "Multipart work will use a separate worktree and reuse your authentication interface. It extends only UploadSession and AssociatedFile in core_models, plus focused router/OpenAPI/changelog edits. Please finalize the auth migration chain first; the upload revision will follow it. Keep shared staging migrations and local port reservations coordinated."

## Corrections to the earlier proposal

1. The documented Python function `transfer_manager.upload_chunks_concurrently()` already uses XML multipart upload. The locally installed library also constructs `XMLMPUContainer`, initiates the upload, submits parts, and finalizes it. Calling this a parallel composite upload was incorrect. Stage 1 confirmed the installed Nano 2.1.2 call path with google-cloud-storage 3.4.1; deployed client versions still need inventory. [Google Python reference](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.transfer_manager)
2. The required IAM permissions for uploading a part, initiating an upload, and completing an upload overlap: each requires `storage.multipartUploads.create` and `storage.objects.create`. An exact-object Credential Access Boundary limits the object, but those permissions do not make completion server-only. A client with the proposed token could also start additional uploads to that object. [XML IAM permissions](https://docs.cloud.google.com/storage/docs/access-control/iam-xml)
3. GCS exposes CRC32C for the completed object, not a SHA-256 value that the API can compare without reading the bytes. A client-supplied SHA-256 value remains an assertion until a trusted process computes it. [Multipart completion reference](https://docs.cloud.google.com/storage/docs/xml-api/post-object-complete)

The broad-token finding in the [security review](parallel_upload_security_review.md) remains valid regardless of which upload protocol the client uses.

## Authorization options and recommendation

| Option | Client changes | What the API can enforce | Limitation |
| --- | --- | --- | --- |
| Exact-object downscoped token | Keep the native Python transfer manager; use the returned exact object name and renewable credentials | Object boundary, limited IAM permissions, API registration after verification | Client can initiate and complete provider uploads; token does not bind to one provider upload ID or part |
| V4 signed URLs for parts | Client uploads bounded parts concurrently using normal HTTP requests | Exact object, HTTP method, provider upload ID, part number, and signed headers | Adds part-signing and resume operations; cannot reuse the transfer manager's complete upload orchestration |

Use signed part URLs for the new route family, as validated in stage 1. They preserve direct parallel transfer while leaving initiation, completion, and abort under API control. Use Google's `Blob.generate_signed_url(version="v4", method="PUT", query_parameters=..., headers=...)`; do not implement signing or canonical request construction manually. [SDK signing API](https://docs.cloud.google.com/python/docs/reference/storage/latest/google.cloud.storage.blob.Blob)

The downscoped-token option is still a viable smaller client migration if client-controlled GCS completion is acceptable. It would require an exact-object condition, no object deletion permission, a unique destination, token refresh with authorization checks, authoritative verification before catalog publication, and reconciliation of multiple provider uploads. Its API must describe completion as verification and registration, not exclusive control of GCS finalization. Do not implement both options initially. [Credential Access Boundaries](https://docs.cloud.google.com/iam/docs/downscoping-short-lived-credentials)

## Proposed API contract

Use `/datasets/{dataset_mfid}/upload/multipart` as the new namespace. This is distinct from the existing `/datasets/{dataset_mfid}/upload/initiate/multipart`. Endpoint names below are proposed. Existing routes and models retain their current meaning during the migration.

| Operation | Proposed route | Contract |
| --- | --- | --- |
| Initiate | `POST /datasets/{dataset_mfid}/upload/multipart/initiate` | Reuse the `UploadInitiate` fields: `filename`, `size`, optional `sha256_hash`; return a new multipart response |
| Authorize parts | `POST /datasets/{dataset_mfid}/upload/multipart/{upload_id}/parts` | Receive a bounded list of part numbers and CRC32C values; return signed PUT URLs, required headers, byte ranges, and expiry |
| Status/resume | `GET /datasets/{dataset_mfid}/upload/multipart/{upload_id}` | Return session state and a paginated list of acknowledged parts; include the existing `AssociatedFileRead` when completed |
| Complete | `POST /datasets/{dataset_mfid}/upload/multipart/complete` | Reuse `upload_id` and `sha256_hash` field names and add required `crc32c` in a new model; return `202` with status location while processing, or `200 AssociatedFileRead` when completed |
| Abort | `DELETE /datasets/{dataset_mfid}/upload/multipart/{upload_id}` | Idempotent cancellation; return `202` while cleanup is pending, `204` when aborted; completed uploads require the normal file deletion workflow |

Keeping completion inside the new namespace avoids changing the legacy completion response to include asynchronous states. The shared implementation can still reuse authorization, validation, and file serialization helpers. Do not dispatch a new session through the legacy completion handler: reject protocol mismatches before inspecting or registering a blob.

The new initiation response should contain `upload_id`, `upload_protocol: "xml-multipart-signed-v1"`, `expires_at`, `part_size`, `part_count`, and `existing_file`. For a verified deduplication hit, return `existing_file` and no active upload. There is no raw access token. Clients obtain concrete URLs from the part endpoint and do not reconstruct GCS paths or need a separate provider upload ID field. A URL necessarily contains that provider ID.

Example, with illustrative identifiers:

```json
{
  "upload_id": "example-upload-id",
  "upload_protocol": "xml-multipart-signed-v1",
  "expires_at": "2026-10-05T12:00:00Z",
  "part_size": 67108864,
  "part_count": 16,
  "existing_file": null
}
```

An optional `Idempotency-Key` header on initiation should bind owner, dataset, protocol, and normalized request fields. A repeat returns the original session; reusing the key with a different request returns `409`. This handles a lost initiation response without adding a required body field.

## Session ownership and object identity

- Require the authenticated session owner and current dataset `CREATE` permission for initiation, part authorization, and completion. This preserves the existing contributor ability to add files. Replacing an existing file is a separate `EDIT` operation and is outside this upload flow.
- Recheck ownership and authorization on status. Permit the owner to abort their unfinished upload after dataset access is revoked so they can stop billing; grant operational cleanup through a separate service identity. Revocation stops new part URLs and publication, but already issued URLs remain usable until expiry or provider abort.
- Allocate an opaque object key per session, for example `uploads/{dataset_mfid}/{upload_id}/content`, with the user filename stored separately. Never derive the destination solely from the filename. The server assigns object metadata and content type.
- Define duplicate logical filenames explicitly: reject a conflicting new upload with `409` in the new workflow and reserve the name transactionally. Existing duplicate records need an audit, not an unreviewed global constraint. A unique storage key still protects concurrent old/new sessions from overwriting each other's bytes; legacy clients may continue creating duplicate logical names during coexistence.
- Record the final GCS generation and use it for verification, reads where version pinning is required, and cleanup. No client-chosen bucket, object name, provider ID, or generation may determine the API's storage target.

GCS multipart completion can replace a same-name object and does not support request preconditions. Unique destinations and restricted write identities are therefore essential. [XML multipart behavior](https://docs.cloud.google.com/storage/docs/multipart-uploads)

## Part transfer, size, and retry rules

Propose a 64 MiB part size and four concurrent transfers as initial client defaults, subject to measurement. At the current 500 GiB application maximum, 64 MiB produces 8,000 parts; the previous 8 MiB resumable hint would produce too many parts. Compute part count and byte ranges on the server. Reject sizes outside the application maximum, and keep the part geometry fixed for the session. GCS permits at most 10,000 parts, with sizes from 5 MiB to 5 GiB except that the final part can be smaller. [GCS limits](https://docs.cloud.google.com/storage/quotas)

Issue at most a proposed 64 URLs per request, valid for 15 minutes or the remaining session lifetime, whichever is shorter. Bind every URL to PUT, the exact object, `uploadId`, `partNumber`, and `x-goog-hash: crc32c=...`. Store the first accepted checksum for a part and reject attempts to change it. Sign the expected content length; stage 1 validated this with Python and Firefox. Browsers must supply Content-Length themselves, so return the expected size separately from headers the client explicitly sets. Test the remaining browser/file-body matrix during integration. CRC32C is not a cryptographic payload or size constraint, and a size check at completion cannot prevent temporary oversized storage.

Use bounded retry with backoff for transient network/provider errors. A retry sends the same bytes and checksum to the same part number. On URL expiry, request fresh authorization for that part after checking session state; never fall back silently to the legacy token route. Avoid reauthorizing every part at once for large files, and bound client memory rather than reading the full file into RAM.

Persist the application upload ID and the source file's size/hash locally so resume cannot accidentally use a changed source file. The server derives progress from GCS rather than trusting client acknowledgements. Google's list-parts operation returns at most 1,000 parts per page, so completion must consume all pages and status must support pagination. [List parts](https://docs.cloud.google.com/storage/docs/xml-api/get-object-multipart)

## Completion, integrity, and recovery

Use a durable state machine: `initiating`, `uploading`, `finalizing`, `completed`, with `aborting`, `aborted`, `expired`, and `failed` terminal/error paths as appropriate. A worker lease or atomic state transition prevents competing completion and abort operations. Do not hold a database transaction open while making long GCS calls.

1. Authenticate, authorize, and atomically claim finalization. Store the completion checksum claims so retries cannot change them. Stop issuing new part URLs.
2. List every provider part and verify the exact part-number set, expected sizes, and total size. Freeze the ordered ETag manifest durably; use the provider values rather than a client-submitted list.
3. Complete GCS using that manifest. Outstanding signed URLs can still be exercised while finalization runs, so correctness must rely on provider ETag validation, signed checksum constraints, and final verification. Do not assume stopping URL issuance instantly freezes GCS parts.
4. Persist the observed object generation and CRC32C. Compare the final size and CRC32C with the session and completion request. On a timeout, reconcile the object and provider state; `NoSuchUpload` alone does not establish success or failure.
5. Adopted integrity policy: provider-validated transport CRC32C guards every part PUT and the completed object's size and CRC32C are compared with the completion claim before publication. The completion SHA-256 is recorded on the associated-file record with a null verification timestamp, excluded from trusted deduplication. A later addition may add a background worker that streams the recorded generation to compute SHA-256 and fills the verification timestamps over time. CRC32C cannot prove an attacker-supplied SHA-256 claim; this policy scales publication without a full-object read on the upload path.
6. Recheck current dataset permission/existence before publication. In one database transaction, create exactly one associated-file record and record its identifier as the session result. Subsequent completion/status requests return that same result.
7. On failure, retain a diagnostic state and reconcile cleanup. A provider completion followed by a failed database commit must be recoverable without creating a second file or deleting an object that was successfully published.

The implemented policy is client-asserted SHA-256 plus verified transport CRC32C. The hash is recorded as unverified and excluded from trusted deduplication. CRC32C cannot prove an attacker-supplied SHA-256 claim. Existing file hashes need verification before the new workflow uses them as trusted deduplication candidates. Deduplication remains dataset-local and subject to read authorization.

GCS completion can take minutes, so return `202` with `Location` and `Retry-After` and perform recovery through durable work records. Do not rely on an in-process background task surviving a pod restart. Use the established worker/queue infrastructure if suitable, with database state or an outbox making scheduling recoverable. [Completion behavior](https://docs.cloud.google.com/storage/docs/xml-api/post-object-complete)

## Database and code impact

Reuse the existing `UploadSession` and `AssociatedFile` tables. A separate route does not require a separate session table. Extend these tables additively and reuse shared API models where their semantics match. No additional session, part, or file-provenance table is required for the initial implementation.

| Existing data to reuse | Additive data for the new flow |
| --- | --- |
| `upload_id`, `owner_user_id`, `dataset_mfid` | Protocol discriminator |
| `gcs_path` | Bucket and provider upload ID |
| `associated_file_name`, `size` | Fixed part size/count |
| `expires_at` | State, processing timestamps, lease/version |
| Existing unique application upload ID | Idempotency key and request fingerprint |
| Session row | Bounded JSON manifest for authorized part checksums and frozen completion ETags |
| `AssociatedFile.sha256_hash`, `size`, `storage_path` | Verification timestamp, CRC32C, and GCS generation |
| Session row | Completion checksum claims, result file MFID, error category, cleanup status |

GCS remains authoritative for uploaded-part progress, obtained through paginated list-parts requests. Persist the authorized checksums and the frozen finalization manifest in a bounded JSON column on the session, limited to the provider's 10,000-part maximum. Use a short database row lock or version-checked update when authorizing part batches so concurrent requests cannot lose updates or change an already authorized checksum. A separate part table can be considered later if measured contention or manifest-update cost warrants it.

Existing and old-client sessions use a null protocol discriminator to mean legacy behavior; do not label historical rows as resumable because the old multipart route also uses this table. New routes set an explicit protocol. New columns must be nullable or have compatible defaults so existing insertion paths keep working. Retain completed new sessions for idempotent retries; legacy completion can continue deleting legacy rows.

Deploy protocol guards to every API instance before enabling new initiation. The legacy completion handler must reject new-protocol sessions before its expiry deletion or storage lookup, and the new handler must reject legacy sessions. Any existing session cleanup must distinguish protocols. Additive columns alone are insufficient for safe mixed-version deployment because an unpatched legacy handler would otherwise accept a new upload ID. Public legacy request/response schemas remain unchanged.

Use typed timezone-aware timestamps for new processing fields and database constraints for idempotency keys and state invariants. Preserve the existing expiry representation in this change unless a separate compatibility fix is necessary. Store durable verification/generation metadata as additive `AssociatedFile` columns so it survives session pruning, without changing the public file response fields. Existing hashes remain unverified until checked. Every changed SQLModel table needs an Alembic revision, chained after the actual repository head at implementation time, including the parallel ORCID work. Validate a single head and avoid editing another feature's migration.

Review `gcs_storage.py`, `uploads.py`, core models, file download signing, ingestion dispatch, deletion handling, configuration, OpenAPI generation, and client fixtures. Existing downloads and ingestion use `storage_path`, which can represent a different bucket and an opaque object key, but worker credentials, GCS mounts, and filename assumptions still need end-to-end testing. Dataset deletion must schedule abort/cleanup durably before removing the identifying session records. This does not authorize broad deletion of existing dataset storage.

## Isolation during gradual migration

A new route is not a storage security boundary. Existing broad tokens can still access new objects if the legacy service account can access their bucket. The current helper also shares credentials between token minting, storage access, and download signing.

Recommend a dedicated destination bucket and API storage identity for the new workflow, with effective IAM grants that deny the legacy token identity access to it. A separate service account is insufficient if the old account has project-level grants covering the new bucket. If existing download code is given read access to the new bucket through the same identity used for legacy tokens, it recreates the disclosure risk. Separate those credentials and test both positive and negative access paths.

Make the new bucket and identity environment-specific. Staging must not default to `mf-storage-prod`. Verify old tokens cannot list, read, create, overwrite, or delete objects in the new destination. If effective IAM isolation cannot be achieved, document that the overlap retains the original exposure and shorten the legacy window accordingly. Neither signed URLs nor a new path can correct unrelated broad credentials.

The server needs scoped authority for provider initiation/completion, listing parts, metadata verification, abort, and appropriate cleanup. The signer stays server-side. Use the existing Google signing library and configured identity; keyless signing requires explicit signing permission and quota planning. Configure private storage and browser CORS for approved origins, PUT, and the required checksum/ETag headers. CORS is browser interoperability, not authorization. [GCS CORS](https://docs.cloud.google.com/storage/docs/cross-origin)

## Expiry, abort, quotas, and observability

Propose seven days for application session lifetime and a longer provider lifecycle fallback, for example eight days; these are operational defaults to validate, not GCS-enforced session expiry. Expiry denies new URLs and finalization and schedules abort. Keep terminal records long enough for retries and audit, then prune according to retention policy.

A scheduled reconciler must cover unfinished uploads, provider uploads created before a database write failed, completed but unpublished objects, and database states left after worker failure. Persist initiation intent first; if a provider call times out, reconcile by the unique key before blindly creating another upload. Cleanup is generation-aware and coordinated with publication so it cannot delete a successfully registered file.

Use `AbortIncompleteMultipartUpload` as a bucket lifecycle fallback. It supports age and prefix/suffix filters and only cleans unfinished multipart uploads; completed orphan objects need separate reconciliation. Validate retention, holds, soft-delete costs, and lifecycle behavior on the intended bucket. [Lifecycle action](https://docs.cloud.google.com/storage/docs/lifecycle#abort-mpu)

Enforce configurable limits on active sessions, reserved bytes per principal/dataset, part-signing requests, and simultaneous verification work. Release reservations only through terminal transitions. Record route/protocol adoption, uploaded bytes, latency, failures, cleanup lag, and legacy token issuance by authenticated client identity. Do not log access tokens, signed URL query strings, authorization headers, or full provider error responses. Return `Cache-Control: no-store` for responses containing upload capabilities.

## Deployment and retirement

1. Confirm the current Nano call path and versions; agree on signed part URLs versus the documented token alternative. Validate signing, part limits, isolation, and failure recovery in a dedicated staging bucket.
2. Add compatible columns to the existing tables and deploy protocol guards to every API instance before enabling the new routes behind a feature flag. Publish the new OpenAPI schemas and client examples. Existing route schemas remain unchanged.
3. Pilot a small client cohort. Compare throughput and retry behavior with the current transfer manager and measure verification costs. Do not silently fall back to the unsafe route.
4. Make the new route the default in maintained clients. Track remaining legacy callers, publish deprecation guidance, and set a retirement milestone based on observed adoption.
5. Restrict legacy initiation to known remaining callers using authenticated identity, not an untrusted client-version header. Gradually remove that allowlist. This limits exposure but does not make legacy tokens safe for those callers.
6. Disable legacy token minting after the final cohort migrates. Wait out the actual maximum issued token lifetime; reconcile provider uploads already created while tokens were valid. Keep the legacy completion route available for a defined drain period for existing application sessions.
7. Retire legacy initiation with a stable `410` response and migration documentation before removing its OpenAPI entry in a scheduled breaking release. Remove old token-minting code and unnecessary IAM grants after the drain period.

Rollback should pause new initiation while continuing status, completion, and cleanup for existing new sessions. Preserve additive schema changes. Do not automatically re-enable broad token issuance.

## Acceptance criteria

- Old routes retain their documented schema throughout coexistence; new and old sessions cannot be finalized through the wrong handler.
- A signed part URL fails after changing method, bucket, object, provider upload ID, part number, or signed checksum header; it cannot authorize completion, abort, listing, or reads.
- Legacy credentials cannot access new storage; new signing responses contain no unrestricted token.
- Different uploads never share a storage key; filename conflicts and trusted deduplication behave as specified.
- Owner mismatch, revoked access, dataset deletion, expired session, and repeated abort are handled consistently.
- Interrupted transfers resume; source-file changes are rejected; URL renewal works; more than 1,000 parts are listed correctly; the maximum configured size fits provider limits.
- Browser CORS and signed content-length behavior are verified with actual clients. Oversized, missing, duplicated, replaced, and corrupt parts fail before publication.
- Concurrent completion, completion versus abort, pod restarts, lost provider responses, and database commit failures converge to one published file or a recoverable cleanup state.
- SHA-256 trust policy is explicit; unverified legacy/client hashes cannot silently become trusted deduplication evidence.
- Downloads, ingestion, and deletion handling work with the new bucket and object keys. Lifecycle and reconciler tests demonstrate cleanup without deleting registered objects.

The provider authorization/isolation experiment has passed against live GCS in disposable buckets; see the [stage 1 results and limitations](parallel_upload_stage1.md). Implementation can proceed with the additive database/storage foundation and contract tests. The production deployment checks and application recovery behavior remain to be established.
