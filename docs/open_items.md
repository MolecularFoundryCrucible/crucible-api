# Known limitations and planned work

This page lists known limitations and work that is neither complete nor scheduled, unless an entry states otherwise. Each entry replaces a longer note that has been removed; the full text is in git history and can be read with `git show 0443fdd:docs/<old file name>`.

## Scheduled removals

### Deprecated routes and fields for 3.x clients

The following remain served for nano-crucible 3.3.0, the ingestion pipeline and Crucible Lens: `POST /users/resolve`, `GET /account/join_requests`, `POST /access_groups/{group_name}/join`, `GET /users/{user_ref}/apikey/verify`, four postprocessing aliases, `/resources/{mfid}/access_groups` and `/users/{user_ref}/access_groups/{group_name}`; the request fields `owner_orcid` and `instrument_name`; the response fields `resource_id`, `dataset_id`, `sample_id` and `group_name`. They will be removed when those clients run nano-crucible 4.x, with the move to `/api/v4`. `resource_id` in the transfer-ownership and project-reassignment responses must remain while 3.x clients exist. The same removal covers offset pagination on `/datasets` and `/samples`, `include_datasets` and the `project_lead_email` column.

History: `routes_audit_2026-10.md`

### Hub sign-in wrappers and settings

`/auth/hub/login`, `/token`, `/refresh` and `/revoke` are deprecated wrappers around the `/oauth/*` routes and will be removed when Hub 1.8.0 is deployed everywhere. `/auth/hub/callback` and the provider option `legacy_callback` will be removed when the Foundry's ORCID application uses `<API>/oauth/callback/orcid`. The `HUB_AUTH_*` settings, the older mechanism for registering the Hub, will be removed when the Foundry's production deployment lists its clients in `CRUCIBLE_AUTH_CLIENTS`.

### Identity lookup setting

`CRUCIBLE_AUTH_IDENTITY_LOOKUP` is a rollout setting. When the Foundry's production deployment runs with it enabled, the setting will be removed and the lookup will be permanently enabled.

### Legacy image paths and cleanup script

The image retains the links `/root/.venv` and `/root/scripts`, and the script `scripts/cleanup_hub_auth.py`, for the Foundry's cleanup CronJob. All three will be removed when that job calls `/app` paths or `python -m app.cli auth cleanup`.

### Plain `apitoken` table

Keys are stored hashed in `api_key` and copied there on first use; the plain `apitoken` table is retained alongside it. Dropping the plain table comprises the two remaining steps of the key hashing plan and depends on the clients' 4.x releases.

History: `api_key_lifecycle_followup.md`

### Secret Manager `NAME_VERSION` secrets

`get_secret` in `app/config.py` falls back to a Google Secret Manager version named in `{NAME}_VERSION`. The fallback remains while the Foundry uses it; secret files are the portable form. It will be removed when the Foundry's platform delivers its secrets as files.

History: `structure_audit_2026-10.md`

### Move to `/api/v4`

The prefix is part of the OAuth issuer, signed links and client settings, so `/api/v3` and `/api/v4` would both be served during a transition. The move coincides with the removal of the deprecated routes and fields above. At the same time, decide whether a disabled feature returns 501, as it does now, or 404.

## Authentication and API keys

### Remaining items from the authentication specification review

Minor independent items: a per-client setting to enable the device grant; rate limits and form validation on the device-code page; `api_key_id` and token-issue entries in the sign-in history; a `crucible_` prefix on keys created by the older routes; `created_by` on `POST /users`; a username availability route; the provider setting for Dex `auth_time`; an in-app notification when a user's logins change.

### API key changes take up to a minute to apply

API key callers are cached per worker for `AUTH_CACHE_TTL` seconds (60 by default), so a revoked key, a lowered role or a removed membership can remain effective for that long on other workers. This was accepted as a bounded trade-off. If changes must apply immediately, the planned fix is a Postgres `LISTEN/NOTIFY` channel that instructs every worker to drop that user's cached entry.

History: `api_key_lifecycle_followup.md`

### Remaining items from the November 2026 security review

Reviewed and left unchanged in 4.0.0, each independent: sign-in forms and cookies have no `__Host-` prefix or `Origin` check (they rely on `SameSite` and signed state); any client can start device sign-ins and login transactions, which are bounded only by their expiry and the cleanup job; signed cookies and forms use the signing library's default digest (HMAC-SHA1), which the 32-character key minimum makes safe but a later change could move to SHA-256; an administrator service account's API key has administrator rights without requesting them; inline previews use a deny list of types where an allow list would be safer; the image's base images are referenced by tag, not digest; the older graph routes have no depth limit; printing and queue publishing run on request threads.

### `max_age` is forwarded, not enforced

A client's `max_age` is sent to the identity provider, and the API records the `auth_time` the provider returns, but does not compare the two when the sign-in completes. Clients check `auth_time` themselves, and the routes that require a recent sign-in check it on the server. Undecided: whether the callback should reject a sign-in older than the requested `max_age`.

### One secret key for several purposes

`CRUCIBLE_SECRET_KEY` signs pagination cursors by default, computes the fingerprints inside every cursor even when separate cursor keys are set, signs password links and device approvals, and keys the stored hashes of client addresses and device codes. Rotating it breaks all of these at once, and the cursor key ring cannot rotate cursors independently. Proposal: keep the single operator secret, derive a per-purpose key with HKDF, and compute cursor fingerprints with the cursor key ring. Deploying the change invalidates outstanding cursors once.

History: `secret_key_separation_followup.md`

### Concurrent registration of lookalike usernames

Two users who register lookalike usernames concurrently can both succeed; the unique index `ix_user_username` rejects only identical names. The risk is low, and no fix is designed.

### Crucible Lens on self-hosted deployments

The redirect address of Crucible Lens is fixed to the Foundry's host, so Lens cannot sign in against another installation. Either Lens makes the address configurable, or the API accepts a custom-scheme redirect for public clients.

## Data model and database

### Drop columns and tables the API no longer reads

`dataset.project_id`, `sample.project_id`, `dataset.instrument_id`, `dataset.instrument_name`, `dataset.sha256_hash_file_to_upload`, `project.project_lead_name` and the `userprojectlink` table are unused but remain in the database. First decide how to handle the 1,142 datasets whose only instrument information is `instrument_name`, and inspect the last two columns on production. A single migration then drops them all. A related idea, not started: one shared handle column on `idtype`, so that a project slug and an instrument slug cannot collide.

History: `legacy_slug_columns_followup.md`, `permission_system_plan.md`

### Rename the `idtype` table to `resource`

The model class is `Resource` but its table is still `idtype`. The change is four lines in `app/tables.py` and one migration (`ALTER TABLE idtype RENAME TO resource`). To avoid a hard cutover, the same migration creates a view named `idtype`, which is dropped in the next release.

History: `idtype_to_resource_rename.md`

### `unique_id` to `mfid`

Most schemas name a record's own identifier `unique_id`, others `mfid`. References to other records already have `*_mfid` names alongside the deprecated ones; a record's own `mfid` alongside `unique_id` is not yet added. Remaining: add `mfid` to the resource schemas and accept either name in requests, migrate the clients, remove `unique_id` in a breaking release, and rename the ORM attributes without renaming columns. Later: MFIDs for every human user (accounts registered with a password receive one; ORCID accounts still use the ORCID). Related naming that predates the current rules ([Resource identifiers](resource_identifier_routes.md)): fields that take a user reference under a bare name (`owner`, `new_owner`, `project_lead`, `accessible_to_user`) are to gain `_ref` forms with deprecated aliases, and `AssociatedFileRead` names its identifier `mfid` and exposes the internal `dataset_id` beside `dataset_mfid`.

History: `mfid_naming_transition.md`

### Enum columns declared as strings

Columns such as `platform_role` and `group_type` are PostgreSQL enums but plain `str` in the models. Adding two `User` or two `AccessGroup` rows in one flush fails on PostgreSQL only, which the SQLite tests do not detect; the code currently adds them one at a time. Fix: declare the columns with their enum type and add a PostgreSQL test that inserts two rows of each affected model.

History: `enum_columns_followup.md`

### Project archiving

An inactive project cannot be hidden without deleting it. Proposal: `PUT` and `DELETE /projects/{project_ref}/archive`, with nullable `archived_at` and `archived_by_user_id` columns. Archived projects are excluded from lists and search unless `include_archived=true`, remain reachable by exact reference, and remain writable; whether an archived project refuses join requests is undecided. Requires a migration and client changes.

History: `project_archiving.md`

### Files on samples and instruments

`AssociatedFile.dataset_id` associates files with datasets only. A generic resource reference would allow samples and instruments to hold files without a new table or route family. It requires a non-trivial migration, and there is no use case yet.

History: `skipped_routes.md`

### Records in several projects

A record shared with a second project is readable by that project's members but does not appear under `?project_id=` for it, because a record has a single project column. Proposal: a `resource_project_link` table that records membership and is written together with the access grant. Deferred because `project_id` is part of the dataset and sample schemas and of client filters.

History: `permission_system_plan.md`

## Files and storage

### Stored objects left behind by deletion

`DELETE /files/{file_id}` and dataset hard deletion remove database rows but not the stored objects, so storage leaks and deleted data is retained. This is the accepted behavior for 4.0.0: removing the record removes the file from its dataset, and object cleanup follows later. Planned: after the rows are committed, retire the object the way a replaced file's content is retired (removed by `storage cleanup` on S3 and by the lifecycle rule on Google Cloud Storage), under the rules that file replacement uses (`retire_if_eligible`): the deployment's own bucket, under the file's dataset, no other record on the same path. Parts of an upload still in progress when its dataset is deleted can also remain, because the session row is deleted with the dataset; the bucket's rule for incomplete uploads removes them. Objects orphaned before the fix require a reviewed script that runs as a dry run first, and explicit authorization.

History: `file_object_cleanup_followup.md`

### Unverified SHA-256 hashes

`sha256_hash` on an uploaded file is the client's claim. Deduplication of new uploads requires `sha256_verified_at`, which nothing in the API sets, so it never applies to them. Proposal: a verification job that reads stored objects, compares the hash and sets the timestamp.

History: `multipart_upload_api.md`

### Stored objects shared between records

In the Molecular Foundry's production data, 102 stored objects are shared by 244 file records, all within one dataset, and 3,769 object keys contain an empty path segment. Both remain valid: a replacement does not retire an object that another record shares, and the storage location rule accepts empty segments. Not done: deduplicate the shared objects, clean up the keys, and check whether the bucket's data-access logs can show past reads across datasets.

History: `storage_download_design.md`

### `file_id` path parameter

The file routes name their path parameter `file_id` although it takes an MFID, and responses have no `file_mfid` field. Rename with the other identifier changes at the move to `/api/v4`.

History: `storage_download_design.md`

### Duplicate file names on GCS records

The unique index on dataset and file name covers only non-GCS records. Production had 52 duplicate pairs over 117 GCS records, most created by the `upload/complete` route removed in 4.0.0, which inserted without checking the name. Clean them up (keep the record that matches the stored bytes, repoint ingestion requests, never delete the object), decide how to handle about 150,000 legacy names that start with `/` or contain `.` segments, then extend the index to all records. `POST /datasets/{dataset_mfid}/files` still accepts such names for GCS records, so a client that writes files to disk by name must validate the name itself; since 4.0.0, renames follow the upload rules.

History: `file_name_uniqueness_followup.md`

## Search, graph and performance

### Lists without pagination

These routes return a complete list: `GET /datasets/{dataset_mfid}/files`, `/thumbnails` and `/keywords`; `GET /keywords` and `GET /keywords/{keyword}`; `GET /resources/{mfid}/links`, `/access` and `/access_groups`; `GET /instruments/{instrument_ref}/service_accounts`; `GET /access_groups/{group_name}/users`; `GET /users/{user_ref}/access_groups`; `GET /account/identities` and `GET /users/{user_ref}/identities`; `GET /pipelines`, `GET /ingestors` and `GET /auth/methods`. Several other collections accept only offset pagination ([Collection queries](get_query_contract.md)). Convert the ones that can grow to cursor pagination.

### Collection query follow-ups

Not implemented: calendar facets (the dataset `timestamp` is producer-supplied text; a facet would use `creation_time` in UTC); a single collection and cursor across datasets and samples; previous-page cursors, range metadata and snapshot pagination; field selection or a query language; related-resource counts and a `last_activity_time` on projects and instruments, in place of propagating `modification_time` to parents. Offset pagination is still accepted on the cursor-paginated collections and sends no deprecation signal. `GET /users` ignores undeclared query parameters with `Deprecation: @1799280000` (2027-01-07) and is announced to reject them with 422 from that date.

History: `get_query_contract.md`

### Slow metadata search ranking for common terms

`GET /resources/metadata/search` finds matches quickly but rebuilds each match's text vector to rank it, so a common term takes seconds to minutes (5.8 s for 1,289 matches). Options: a stored `tsvector` column populated in batches, ranking only the first N matches, indexing values without keys, and a statement timeout in every case. Decide together with the direction for embedding search. The table also occupied 66 GB for about 0.6 GB of live data; reclaiming the space is a production operation that requires authorization.

History: `metadata_search_followup.md`

### Consolidated graph routes

nano-crucible, the Graph Explorer and Crucible Labs still call `GET /entity_graph_cte/{mfid}` and `GET /project_graph/{project_ref}`, alongside the newer `GET /resources/{mfid}/graph`. Proposal: add a full-component mode and a project mode to `/resources/{mfid}/graph`, migrate nano-crucible to it, keep the old routes as wrappers for one release window, then remove them. Open decisions: whether traversal may cross project boundaries, whether a project graph requires read access on the project itself, and whether to keep the NetworkX node-link format. `/project_graph` takes about 3 s for the largest projects; skipping `jsonable_encoder` or adding an `ETag` would reduce this.

History: `graph_routes_migration.md`, `project_graph_performance_followup.md`

### Cross-type search

The Hub search page calls each `/<type>/search` route and merges the results itself. A `GET /search?q=&type=` route with ranked mixed results would sit alongside the per-type routes. A related idea is a mixed-type `GET /resources` summary collection, filterable only on fields common to every type. Implement either when a use case arises that the typed collections cannot serve.

History: `user_representation_and_search_followup.md`, `mfid_naming_transition.md`

## Ingestion, printing and metrics

### Message queue publishing

`send_pika_message` opens a RabbitMQ connection within the request with no timeouts and does not close it on error, so an unreachable or blocked broker can hold workers. A crash between the database commit and the publish leaves a request recorded but not queued, and ingestion request statuses are free text. The rework should add timeouts and a `with` block, define the status set with the consumers, and decide on a transactional outbox.

History: `message_queue_publishing_followup.md`

### List of ingestors

`GET /ingestors` returns a hard-coded, outdated list. Proposal: the ingestion consumer registers its available ingestors, and the route returns those.

### Printer registry

`GET /print/printers` opens an MQTT connection on every request and waits for retained statuses, which is too slow for search or autocomplete. Proposal: a `printer` table written by a single status subscriber, with the API reading only the database; the route gains `q`, an `online` filter and pagination, and returns 503 when the writer is down.

History: `printer_registry_followup.md`

### Metrics: per-worker counters and public route

Each gunicorn worker keeps its own Prometheus counters and `/metrics` responds from whichever worker receives the request, so request counts and rates are inflated and unreliable. Fix: `prometheus_client` multiprocess mode (`PROMETHEUS_MULTIPROC_DIR`, `MultiProcessCollector`, a `child_exit` hook), or one worker per pod. `/metrics` is also reachable without authentication on the Foundry's public address and lists every route with its traffic; the Compose bundle returns 404 for the public path since 4.0.0, and the Foundry's nginx requires the same rule (in the deploy repository).

History: `prometheus_multiprocess_followup.md`, `public_metrics_endpoint_followup.md`

## Access and sharing

### Collections

Sharing N records takes N calls, shares do not follow lineage or new records, and grants have no expiry, grantor or timestamp. Proposal: a collection is a named access group that holds a role on each record added to it; users and projects become its members, so the permission check does not change. Open questions: live or one-time lineage, whether collections replace custom groups, who may create them, and how the Hub presents them.

History: `sharing_collections_followup.md`

### Records without any grant

On production about 180,600 datasets and 242 samples have no grant, owner, project or instrument, so only platform administrators can read them; 37,537 `idtype` rows of type dataset reference no dataset. Options: attribute the datasets by name pattern, upload path or ingestion history, or leave them administrator-only; delete the orphaned `idtype` rows after checking the deletion history. The counts have not been re-verified.

History: `sharing_collections_followup.md`

### Project sharing policy and classification tags

Any user with `admin` on a record, which includes every project admin, can share it outside the project or publish it, and no project setting can prevent this. Proposal: a per-project level (`open`, `restricted`, `closed`) enforced when a grant is created, never when access is evaluated, with `open` as the default; it should also cover whether the project is discoverable. A broader idea is a tag set per record (`sensitive`, `embargoed`, `proprietary`) with rules on who may apply or remove a tag.

History: `permission_system_plan.md`

### Service accounts and instruments

A service account can create a dataset for any registered instrument in any project: a missing instrument binding is only logged as a warning, and instruments are not associated with projects. Next steps: return 403 once production logs show that every active service account is bound, and add `POST` and `DELETE /instruments/{id}/projects/{project}` before narrowing service-account access. A service account's claim to act on behalf of a researcher is trusted; device sign-in, where the researcher approves from their own phone, would harden it.

History: `permission_system_plan.md`

### Open policy questions

Three decisions that change an authorization rule without reshaping a route: whether a platform administrator may transfer ownership on behalf of an owner who has left; whether a group can own a record; and whether to use the reserved `support` platform role for a read-only auditor. Decided in 4.0.0: any signed-in user may create a project that they lead; only an administrator can create one for another user or one that participates in proposal sync.

History: `permission_system_plan.md`

### Elevated mode: remaining work

An administrator's API key is elevated when the `Crucible-Privilege-Mode` header is omitted, a compatibility default. Proposed, not scheduled: an omitted header means normal mode in a future major version, once clients send the header; short-lived, server-issued privilege leases with recent authentication, expiry, revocation and an optional justification; a database audit history in addition to the request log; and a defined rule for how the mode applies to operations that outlive the request.

History: `explicit_privilege_mode_design.md`

### Join request notifications

Requesters and reviewers are not notified when a join request is created or decided; clients poll `GET /join_requests`. Proposal: in-app notifications with their own table and routes, delivered by polling first and server-sent events later. Email is out of scope.

History: `join_requests_design.md`

### Audit log for permission changes

Grant changes and platform-administrator bypasses are not recorded. Proposal: a `ResourceAuditLog` table with one row per change (actor, action, target).

History: `permission_system_plan.md`

### Access group list and delete routes

`GET /access_groups` and `DELETE /access_groups/{name}` do not exist. Listing requires a rule for which identity, project, instrument, custom and system groups each user can see. Deletion requires a choice between removing the group's grants and memberships or rejecting deletion of a group in use, and must never remove entity-owned or system groups. No use case yet.

History: `skipped_routes.md`

### Unauthenticated routes

`GET /ingestors` responds without authentication; it returns a fixed list. The keyword routes require authentication since 4.0.0.

History: `routes_audit_2026-10.md`

## Code structure

### Split the large modules

Route modules contain most of the logic and import each other's private helpers: `datasets.py` is about 1500 lines, `resources.py` 1250, `deletion.py` 1070, `users.py` 1000. After the release, split them by sub-resource, one pull request each, with an unchanged OpenAPI document as the check, and move query logic into `app/services`. `app/utils/auth_utils.py` (about 800 lines) mixes credentials and authorization, and about 150 imports are placed inside functions without a technical reason.

History: `structure_audit_2026-10.md`

### Session dependency and shared error responses

Routes open `Session(get_engine())` directly in about 180 places. Proposal: a `get_session` dependency and a `SessionDep` alias, one router per pull request, then `app.dependency_overrides` in tests in place of the `use_engine` fixture. Error descriptions repeated across routes should move into `app/api/errors.py` as named constants, with one helper such as `error_detail(code, message)` for coded error bodies. Neither change may alter the OpenAPI document.

History: `database_engine_followup.md`, `error_responses_followup.md`

### Naming, module locations and repository maintenance

`app/api/routes/datasets.py` imports `start_postprocessing_run` from another route module; `app/utils/gcs_record_utils.py` is no longer specific to GCS; `app/general_utils.py` belongs in `app/utils`; service-like code in `app/utils` belongs in `app/services`. The `uv` version is pinned in the Dockerfile and in each workflow, Dependabot does not monitor the CI service images, and `.gitignore` is a generic template. `migrations/data` holds 46 one-time scripts that can be archived once it is known which ones ran in production.

History: `structure_audit_2026-10.md`

### Test setup and coverage

Test setup is duplicated across files: password settings in 13 files, a Postgres engine fixture defined 7 times, and four database URL variables (`CRUCIBLE_TEST_PG_URL`, `HUB_AUTH_TEST_DATABASE_URL`, `SLUG_INDEX_TEST_DATABASE_URL`, `EMAIL_INDEX_TEST_DATABASE_URL`). Replace them with shared fixtures and a single variable. The routes audit also listed routes with no HTTP test, such as thumbnail `PATCH` and `DELETE` and `POST /access_groups`, and routes tested only as function calls; the list has not been re-verified.

History: `structure_audit_2026-10.md`, `routes_audit_2026-10.md`

## Client requests

### Conditional and idempotent writes

nano-crucible requested `ETag` and `If-Match` on dataset and sample `PATCH`, so that concurrent edits do not overwrite each other, and `Idempotency-Key` on creates, so that a retried request does not create a second record. Undecided.

### Deprecation signals and minimum client version

nano-crucible requested `Sunset` and deprecation link headers, and a minimum client version published through `GET /clients`. Undecided. Currently only some deprecated routes send the `Deprecation` header (the postprocessing aliases and `POST /access_groups/{group_name}/join` do not).

History: `routes_audit_2026-10.md`

### Consistent response shapes

User routes return either `UserRead` or `UserPublicRead`, so a `GET /users` page mixes two shapes; the proposal is one schema whose private fields are `null` when hidden, with `user_capabilities` distinguishing hidden from empty. Across the API, about 15 routes return bare lists where others return `PaginatedResponse`, errors are a plain string on most routes and `{code, message}` on others, and paths mix snake_case and kebab-case. A single list envelope, a single error body (possibly RFC 9457) and a single path style are breaking changes for `/api/v4`.

History: `user_representation_and_search_followup.md`, `routes_audit_2026-10.md`, `error_responses_followup.md`

### Overlapping routes

Agree with nano-crucible before changing these: `GET /health` (an alias of `/health/ready`), `GET /users/{user_ref}/datasets/{dataset_mfid}`, and `POST /resources/{mfid}/delete` (requests deletion) alongside `DELETE /resources/{mfid}` (hard deletion).

History: `routes_audit_2026-10.md`
