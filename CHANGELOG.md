# Changelog

This changelog records changes that affect API users, operators, or downstream clients. Add entries under `Unreleased` in the same change that modifies the observable contract or operational behavior. Internal-only changes do not require an entry.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Breaking changes

- Instruments have members, like projects: `GET`, `POST`, `PATCH`, `DELETE /instruments/{instrument_ref}/users/{user_ref}` (roles `viewer`, `editor`, `admin`); user and project grants on an instrument through `/resources/{mfid}/access/...` return 409 `use_instrument_members`. Needs migrations `3b8e5d1f7c24` and `9d4a2c7e6f13`.
- Every instrument member reads the datasets assigned to the instrument, at `viewer` and never more; the migration makes today's owners, admins and editors members.
- Transferring an instrument keeps the previous owner as an `admin` member, and a service account cannot own an instrument.
- `instrument_name` is no longer an input: dataset create and update return 422 for a name (`null` is ignored), and the `instrument_name` filter on `GET /datasets` returns 422; use `instrument_id` or `instrument_mfid`.
- Records have one shape: `owner` (a `UserPublicRead`) on every record and by default on lists and search, project `resource_type`, `role` on datasets and samples, a typed `deletion_request`, and the project member shape for instrument `members`.
- Parent and child routes and a sample's embedded `datasets` return full responses without nested links; sample `include_datasets` defaults to false.
- `capabilities.can_delete` follows direct deletion (owners only): true for dataset and sample owners, false for project and instrument admins who are not the owner.

### Added

- `affiliation=instrument` on dataset lists, facets and search, for datasets read through instrument membership; `affiliation=member` on instruments.
- `python -m app.cli instruments check [--commit]` reports, and repairs, instrument grants that do not match membership and dataset assignment.
- `owner_ref` names a user in create bodies, `new_owner_ref` in ownership transfer; the owner filter `owner_ref` is repeatable and exists on project lists and search too.

### Deprecated

- Request names `owner`, `owner_orcid`, `project_lead*` and `new_owner`, replaced by `owner_ref` and `new_owner_ref`; the `owner_id` filter, replaced by the repeatable `owner_ref`; `owner_id_is_null`, replaced by `owner_is_null`.
- Response fields `owner_orcid`, `lead`, `project_lead_orcid`, and on datasets and samples `project_id`, `instrument_id` and `instrument_name`, replaced by the nested `owner`, `project` and `instrument`.

### Changed

- The Docker Compose installation guide is a quick start; HTTPS, sign-in methods, the Hub, file storage, operations and troubleshooting each have a page in `deploy/compose/docs/`.
- The pages that set a password (password link, registration) check the length rule and the confirmation as the user types, and their hints and messages are shorter.
- With passwords enabled, the sign-in page is the username and password form, with the listed identity providers offered below it; the separate method-choice page remains only for several providers without passwords. `provider=` still goes straight to one method.
- The sign-in pages served by the API (method choice, password sign-in, password link, registration, program and device approval, result pages) share one layout: a narrower card, larger controls, boxed error messages, request details as a list, and a full-width layout on phones.

## [4.0.0a1] - 2026-11-11

Pre-release of 4.0.0 for testing; the final release keeps this list.

The API signs users in itself: it is an OAuth 2.0 authorization server with several identity providers, passwords, device sign-in and named API keys, and it no longer depends on the Hub or on ORCID to do so. All routes remain under `/api/v3`. Operators and client authors: follow [Upgrading from 3.3.1 to 4.0.0](docs/upgrading/3.3-to-4.0.md). Database migrations `7c1e4a9d2f60` to `c41d7e9a52b6`.

### Breaking changes for operators

- Sign-in settings: `CRUCIBLE_AUTH_PROVIDERS` with `OIDC_{KEY}_CLIENT_SECRET` replaces `HUB_AUTH_ORCID_*`, `ORCID_CLIENT_ID` and `ORCID_CLIENT_SECRET`; `CRUCIBLE_AUTH_CLIENTS` with `OAUTH_{KEY}_CLIENT_SECRET` replaces `HUB_AUTH_*`, and setting both stops the server.
- `API_PUBLIC_URL` and `API_PATH` replace the YAML settings files in the image and `API_CONF_FILE`.
- `CRUCIBLE_SECRET_KEY`, the cursor signing keys and every client secret must be at least 32 characters; the server refuses to start otherwise.
- Identity providers return to `<API>/oauth/callback/{key}`; `"legacy_callback": true` keeps `<API>/auth/hub/callback` for an ORCID application registered earlier.
- After the migration, run `python -m app.cli identity backfill` and `python -m app.cli owners backfill` once, then set `CRUCIBLE_AUTH_IDENTITY_LOOKUP=true`.
- Uploads need `MULTIPART_UPLOAD_ENABLED=true`; `LEGACY_UPLOAD_TOKEN_DOWNSCOPE` is no longer read.
- The image runs as an unprivileged user (uid 10001) with its code in `/app`; a read-only root filesystem needs `/tmp` and `/dev/shm` writable.
- Request bodies are limited to 10 MiB (`API_MAX_BODY_MIB`) and 20 MiB for thumbnails (`API_MAX_THUMBNAIL_BODY_MIB`), returning 413 `body_too_large`; thumbnail sources to 14 MiB.
- Compose bundle: reachable only from the local host unless `CRUCIBLE_BIND_ADDRESS` is set; plain HTTP is refused for any name but `localhost`; passwords are enabled by default and need the new secret `CRUCIBLE_AUTH_PEPPER`; ORCID sign-in moved to `compose.orcid.yaml`; Docker Engine 28 is required.

### Breaking changes for clients

- Removed, returning 410 `route_removed` with the replacement named: the browser pages that issued a single API key (`/auth/login`, `/auth/callback`, `/auth/apikey`, `/auth/apikey/regenerate`, `/login`, `/auth`, `/user_apikey`).
- Removed: the single-key routes `GET /account/apikey`, `POST /account/apikey/regenerate`, `POST /users/{user_ref}/apikey` and `POST /users/{user_ref}/apikey/regenerate`; use `/account/apikeys`.
- Removed: the resumable and token upload routes `POST /datasets/{dataset_mfid}/upload/initiate`, `/upload/initiate/multipart` and `/upload/complete`; use `/upload/multipart`.
- Removed: `POST /datasets/{dataset_mfid}/files/{file_id}/ingest`, `GET /datasets/{dataset_mfid}/samples`, `GET /users/by-username/{username}`, `GET /users/{user_ref}/datasets` and `GET /users/{user_ref}/samples`.
- `POST /datasets/{parent}/children/{child}` and `POST /samples/{parent}/children/{child}` return MFIDs and `relationship_type` instead of internal ids; the keyword routes no longer return `id`.
- Five OpenAPI schemas are renamed: `DatasetLinkRead`, `SampleLinkRead`, `KeywordRead`, `ResourceAccessGroupRead`, `IngestionRequestRead`.
- `GET /keywords` and `GET /keywords/{keyword}` require authentication.
- `POST /projects`: only an administrator may name another user as the lead (403 `lead_must_be_caller`), and a service account cannot be a lead (422).
- `PATCH /datasets/{id}` and `PATCH /samples/{id}` refuse `public` (422); use `PUT` or `DELETE /resources/{mfid}/access/public`.
- `PATCH /ingestion_requests/{id}` changes only `status`, `time_completed` and `ingestion_githash`; `ingestion_class` must be 1 to 128 letters, digits, dots, hyphens or underscores.
- Changes to human administrators (their platform role, their profile, their keys) and promoting a service account to administrator need a browser session with a recent sign-in; an API key gets 403 `hub_session_required`.
- Starting an upload on a server with uploads disabled returns 501 `feature_disabled` instead of 503.
- `POST /users/resolve` and `POST /datasets/first_thumbnails` accept at most 1000 entries per list.
- `POST /users` no longer creates an API key.

### Added

- OAuth 2.0 authorization server: `GET /oauth/authorize`, `POST /oauth/token`, `POST /oauth/revoke` and metadata at `/.well-known/oauth-authorization-server`, with PKCE, rotating refresh tokens and `iss` on every answer.
- Client registry `CRUCIBLE_AUTH_CLIENTS`: confidential clients with a secret, and public clients such as the nano-crucible command line (PKCE, loopback redirects, a confirmation page).
- Several OpenID Connect identity providers (`CRUCIBLE_AUTH_PROVIDERS`), each with a registration mode (`closed`, `approval`, `open`); `GET /auth/methods` lists the sign-in methods.
- Device sign-in (RFC 8628): `POST /oauth/device_authorization` and the approval pages at `/oauth/device`.
- Password sign-in (`CRUCIBLE_AUTH_PASSWORD_ENABLED`): Argon2id with a pepper, attempt limits per account and per address, one-time password links, `POST /account/password`, `DELETE /users/{user_ref}/password`, and registration that waits for approval (`CRUCIBLE_AUTH_PASSWORD_REGISTRATION`).
- Linked identities: a user links, lists and removes sign-in methods (`/account/identities`, `/oauth/link/*`); administrators use `/users/{user_ref}/identities`. User objects include `orcid`, `orcid_verified` and `sign_in_methods`.
- Named API keys: `POST`, `GET`, `PATCH` and `DELETE /account/apikeys`, stored as hashes; administrators list and revoke with `/users/{user_ref}/apikeys`.
- Sessions: `GET` and `DELETE /account/sessions`; administrators use `/users/{user_ref}/sessions`.
- Sign-in history: `GET /account/auth-events` and, for administrators, `GET /users/{user_ref}/auth-events`.
- Accounts: `GET /users?pending=true`, `POST /users/{user_ref}/approve` and `/reject` for waiting accounts; `POST /users/{user_ref}/disable` and `/enable`; reserved usernames; every new user gets a username.
- `GET /features` lists the optional features of a deployment and whether each is enabled.
- Dataset `capabilities` include `can_delete_files`.
- Projects have `proposal_sync`, and `RESERVED_PROJECT_ID_PATTERN` reserves a project naming scheme for administrators (403 `project_id_reserved`); proposal sync adds users only to such projects.
- Server commands: `db sequences`, `identity backfill`, `identity move`, `owners backfill`, `admin password-link`, `admin set-role`, `admin pending`, `admin approve`, `admin reject`, `auth cleanup`, `auth end-sessions`, `auth revoke-passwords` and `auth rewrite-issuer`.
- Compose bundle: an hourly `auth-cleanup` service, files in `deploy/compose/local/` to adjust certificates and trusted proxies, and the nano-crucible client registered by default.
- The project is released under the BSD 3-Clause License.

### Changed

- `affiliation` combines with an exact owner filter as an intersection; the combination returned 422 before.
- The `owner_id` filters accept a unique_id, linked ORCID, username or email; wherever the API takes a user, an ORCID also finds a user through a linked identity.
- Dataset and sample owners are read from the owner's user record; a stored owner that matches no user is shown as none, and instrument `owner_orcid` can be null.
- `public` in responses and stored records is read from the public access grant, and publishing updates the stored record.
- `POST /account/sync-projects` uses the user's linked ORCID, returns 502 when the proposal service is unreachable, and skips projects pending deletion.
- Samples, projects and instruments record the API version that wrote them, as datasets do.
- Generated usernames keep accented and non-Latin names readable.
- Migrations commit one by one and wait at most `ALEMBIC_LOCK_TIMEOUT` (5 s) for a table lock.
- The `/auth/hub/*` routes are deprecated wrappers around the `/oauth/*` routes, kept for Hub releases before 1.8.0.
- The API warns at startup when its public address is plain HTTP on anything but localhost.

### Fixed

- Editing a dataset no longer publishes or unpublishes it.
- `POST /users` returns 409 for a duplicate or for an ORCID linked to another user, also under simultaneous requests, and checks before calling the proposal service.
- Provider discovery works for issuers with a trailing slash.
- Large JSON bodies, the device approval page and the start of linking no longer block other requests on the same worker.

### Removed

- The `crucible_login_session` cookie and the `<API>/auth` ORCID redirect, with the browser key pages.
- The one-off data scripts in `migrations/data/`; data steps for upgraded installations are server commands.

### Security

- A device approval is accepted only from the user who signed in for it.
- Ending all of a user's sessions also cancels sign-ins that were approved but not finished.
- The refresh grace applies only to a token its holder used, so a copied token cannot keep renewing itself.
- Sessions approved with a device code cannot create keys, end sessions or change sign-in methods (403 `browser_sign_in_required`).
- Sign-in refuses ID tokens that list another audience or issuer, and uses one browser cookie per sign-in.
- The per-address attempt limit delays for at most a minute, so one user behind a shared address cannot lock out the others.
- API keys cannot reach the server log.
- Renaming a stored file follows the upload filename rules; `%` and `_` in a user search match as text; MFID and ORCID checks reject a trailing newline; a malformed `POST /account/password` is answered without repeating the submitted values.
- Compose bundle: `/api/v3/metrics` is not served to the outside, containers have a read-only root filesystem, and the guide encrypts database backups.
- Dependencies: Authlib 1.9.5 and `pyasn1` 0.6.5 with their security fixes.

## [3.3.1] - 2026-11-02

Maintenance: a much smaller image, SQLAlchemy 2.1, and the removal of a route that rotated API keys on a GET. No migration.

### Removed

- `GET /regenerate_apikey`, deprecated since June 2026: it rotated the key on a GET with the session cookie, so any page could trigger it. Use `POST /auth/apikey/regenerate` (browser) or `POST /account/apikey/regenerate` (Hub).

### Changed

- SQLAlchemy 2.1, which makes psycopg 3 the default for `postgresql://` URLs; the server names `postgresql+psycopg2://`, and test database URLs (`CRUCIBLE_TEST_PG_URL` and the others) must too.
- cachetools 7.
- The image builds on `python:3.11-slim-trixie` without unused libraries (cloudevents, h5py, imageio, networkx, numpy, pg8000, pytz, nano-crucible): about 330 MB instead of 1.4 GB.
- The OpenAPI document is no longer tracked; CI generates it on each merge to `main` (workflow artifact) and attaches `crucible-api.json` and its digest to each GitHub release.
- The image no longer takes a `BRANCH_NAME` build argument; `/health` still reports `BRANCH_NAME` when set at runtime.

## [3.3.0] - 2026-10-30

File storage for self-hosting: an S3 adapter, SeaweedFS in the Compose bundle, kept versions of replaced files, and Google storage without a key.

### Upgrading from 3.2.1

- Database: migration `564d0bd33a5c` (one new table, `file_versions`), applied by `python -m app.cli db upgrade` before deploying; then `EXPECTED_DB_REVISIONS` is `564d0bd33a5c`.
- Google storage: the server no longer reads `GCS_SA` and acts as `MULTIPART_UPLOAD_SIGNER` for every storage operation, so the signer needs object access on the record bucket (and read access on `STORAGE_READ_BUCKETS`). Remove `GCS_SA_VERSION`; `settings check` reports it.
- Clients: the legacy upload routes are deprecated; self-hosted servers on S3 need nano-crucible 3.3.0 or later.

### Added

- S3-compatible storage: `STORAGE_BACKEND=s3` with `S3_ENDPOINT_URL`, `S3_PUBLIC_URL`, `S3_REGION`, and the secrets `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY`.
- `python -m app.cli storage check` tests the configured storage end to end.
- On `s3`, replaced files are kept for `FILE_VERSION_RETENTION_DAYS` (14), restorable with `python -m app.cli files restore`, and deleted afterwards by `python -m app.cli storage cleanup` when `STORAGE_CLEANUP_ENABLED=true`.
- Empty files on the signed multipart upload (`size` 0, no parts).
- Compose bundle: SeaweedFS (profile `bundled-storage`), `CRUCIBLE_FILES_DIR`, a `files-backup` service, the hourly cleanup (on by default), and a Google bucket option (`CRUCIBLE_STORAGE_BACKEND=gcs`); see `deploy/compose/README.md`.

### Changed

- On `gcs`, the server uses Application Default Credentials, impersonating `MULTIPART_UPLOAD_SIGNER` when it is set; `MULTIPART_UPLOAD_PROJECT` is optional.
- `GET /datasets/{dataset_mfid}/download_links` signs the links in parallel.
- On `s3`, the legacy upload routes answer 501 `legacy_upload_unavailable`, and a file without a stored location answers 404 instead of 500.
- `STORAGE_BACKEND=s3` refuses a Google `S3_ENDPOINT_URL`.
- `S3_ACCESS_KEY_ID` and `S3_SECRET_ACCESS_KEY` must be one line.
- The Compose bundle runs the Hub from its published image (1.7.1) and builds the API image once.

### Deprecated

- `POST /datasets/{dataset_mfid}/upload/initiate`, `/upload/initiate/multipart`, and `/upload/complete` (with a `Deprecation` header; their request log lines name the client's User-Agent): use `/upload/multipart`.

### Removed

- `GCS_SA`: use Application Default Credentials (`GOOGLE_APPLICATION_CREDENTIALS` for a key file).

### Fixed

- A Google storage error during multipart completion or abort, or while writing an empty file, answers 503 instead of 500.
- An empty-file upload whose SHA-256 is not the empty hash is refused when it starts (422).
- An abort or the cleanup job during completion no longer leaves the assembled object in storage.

## [3.2.1] - 2026-10-28

Dependency upgrades, a deployment settings check, and release images. No migration; responses unchanged.

### Added

- `python -m app.cli settings check` checks rendered Kubernetes manifests against the startup checks (`--server`, `--database`, `--verify-secrets`).
- Release images: a `vX.Y.Z` tag builds one image in `crucible-api-releases` with `cloudbuild.release.yaml`.

### Changed

- Dependency upgrades, including FastAPI 0.162, uvicorn 0.54, gunicorn 26.2, and SQLModel 0.0.48; response field names are kept.

## [3.2.0] - 2026-10-28

Groundwork for self-hosting, plus join requests, postprocessing pipelines, activity tracking, and preferences. Exact shapes are in the OpenAPI document.

### Upgrading from 3.1.0

- Operators: settings are explicit, given one way each, and checked at startup. Required: `DB_SERVER`, `DB_USERNAME`, `DB_DATABASE`, `PGDB_PW`, `CRUCIBLE_SECRET_KEY`. Printing, ingestion, and proposal sync need `PRINTING_ENABLED`, `INGESTION_ENABLED`, `PROPOSAL_SYNC_ENABLED`. `CLIENT_SECRET` is now `ORCID_CLIENT_SECRET`. The container listens on `PORT`.
- Database: migrations `5582366c0eb4`, `d7b51bd885fc`, `b644cf2e7240` (then `migrations/data/20261014_backfill_activity_timestamps.py --commit`), `aa8eb05c643d`, `c3f1d29a7b64`, `5be2f0c8d4a1`, `9d41c6a2e7f3`, `b8f2d6e41c07`, applied by `python -m app.cli db upgrade`.
- Clients: `GET /users/search` returns `{limit, items}`; plan multipart parts from `part_size`; filenames are unique per dataset; timestamps are UTC with `Z`; see Removed.

### Added

- Batch lookup by repeatable `mfid` on dataset, sample, project, and instrument lists, and by `unique_id` and `username` on `GET /users`.
- `affiliation=member` on lists, search, and facets.
- Join requests: `/projects/{project_ref}/join_requests`, `/join_requests`, review by project editors.
- Postprocessing pipelines: `GET /pipelines`, `POST /datasets/{dataset_mfid}/postprocess`, `/postprocessing_runs`.
- Activity tracking: `last_active_at`, `last_used_at`, `last_activity_at`, with sorting and filters.
- `/account/preferences` and `/account/recent-resources`.
- Caller capabilities on members, grants, and resources (`can_remove`, `assignable_roles`, `can_view_access`, and others).
- `GET /print/printers` with online status.
- Self-hosting: `python -m app.cli db upgrade`, `python -m app.cli admin create`, and a Docker Compose bundle with an optional Hub (`deploy/compose/`).

### Changed

- Settings are read once at startup with pydantic-settings, from `NAME`, `NAME_FILE`, the secrets directory, or `NAME_VERSION`.
- Foundry hosts, buckets, and Secret Manager paths are no longer built in; `RECORD_BUCKET` names the record bucket.
- Disabled features answer 501 `feature_disabled`; no upload bucket answers 501 `storage_not_configured`.
- Workers read every needed secret before serving and refuse to start if one fails.
- The image listens on `PORT` with `WEB_CONCURRENCY` workers and `--timeout 60`.
- `GET /health/ready` accepts a schema ahead of the build and reports `schema_state`.
- `POST /files/{file_id}/ingest` answers 503 `ingestion_unavailable` when the broker is down.
- One rule for granting access: up to your own role; ownership moves only through `transfer_ownership`.
- Access lists are readable by anyone with a non-public role on the resource.
- Filenames are unique within a dataset (409 `filename_exists`); non-GCS filenames must be canonical paths.
- Multipart part size depends on the file (8 to 64 MiB).
- Datasets and samples link to projects and instruments by MFID only.
- Records pending deletion: lookups work, lists hide them, writes return 409 `pending_deletion`.
- Timestamps are UTC; PATCH rejects `null` for required fields; lineage rejects self-links and cycles.
- Faster owner filters, graphs, and project and instrument deletion.

### Deprecated

- `POST /users/resolve`, the `unique_id` filter on datasets and samples, and `GET /users/{user_ref}/apikey/verify` (with a `Deprecation` header).
- `POST /access_groups/{group_name}/join` and `GET /account/join_requests`: use the join request routes.
- The per-pipeline dataset processing routes: use `POST /datasets/{dataset_mfid}/postprocess`.

### Removed

- `GET` and `PATCH /users/me` (use `/account/profile`) and `GET /samples/{sample_mfid}/datasets`.
- Nine legacy graph routes (use `/resources/{resource_mfid}/graph`, `/entity_graph_cte/`, or `/project_graph/`).
- `POST /extract_metadata`, `requirements.txt`, `GCS_SECRET_STORE`, and `CLIENT_SECRET`.

### Fixed

- Renaming a project returned 500.
- Moving a record to another project deleted its shares.
- Races around dataset deletion, concurrent metadata writes, and deleted projects or instruments.
- Several 500s on join requests, linking, and key verification; invalid JSON bodies return 422.
- `If-Match` accepts weak ETags (Cloudflare no longer causes 412).
- Staging wrote record files to the production bucket.

### Security

- Dependency upgrades; python-jose replaced by joserfc.
- `POST /extract_metadata` allowed server-side requests to any URL; removed.
- Legacy processing routes now require contributor access.
- Project roles can no longer be granted through resource grants.
- Legacy ORCID sign-in returns only to local paths.
- Storage errors no longer expose bucket URLs or service-account emails.

## [3.1.0] - 2026-10-13

### Added

- Added file content replacement: `POST /files/{file_mfid}/upload/multipart/initiate` starts a multipart replace of an existing GCS file (EDIT on its dataset, optional `If-Match` with the file's ETag or `*`, optional `Idempotency-Key`); parts, status, abort, and completion use the dataset's existing multipart routes, and completion returns the same file record with new content. A stale `If-Match` returns 412 `etag_mismatch`, a concurrent replace 409 `replace_in_progress`, a change during the upload 409 `file_changed`, and catalog-only records 400. Replaced legacy files move to the `{dataset_mfid}/_files/{upload_id}` layout. The previous object is retired by setting its `customTime`, and a bucket lifecycle rule deletes it after seven days. Requires migration `f3b7c2a91d04`.
- Added `etag` to file responses and the `ETag` header to `GET /files/{file_id}`, derived from the stored path, generation, SHA-256, and size; renaming a file does not change it.
- Added `POST /account/apikey/regenerate` and the administrator route `POST /users/{user_ref}/apikey/regenerate`, which issue a new API key, revoke the previous one, and return the new key once.
- Added an optional `max_age` parameter to `GET /auth/hub/login`, forwarded to ORCID (`0` also sends `prompt=login`) so the Hub can force a fresh ORCID sign-in. The ORCID `auth_time` is recorded on Hub login codes and token families (migration `d94e1f2a7b05`).
- Ingestion queue messages now include `file_name`, the file record's display filename, so consumers no longer depend on the object key for names or types.
- Added a separate signed XML multipart upload API (`/datasets/{dataset_mfid}/upload/multipart/...`) with synchronous initiation and completion, bounded per-part URL authorization, resumable status, idempotent initiation and cancellation, and per-part and whole-object CRC32C verification. The API performs all storage calls inside the request, and a bucket lifecycle rule cleans abandoned uploads. New initiation defaults to disabled; legacy upload contracts remain supported. The completion SHA-256 is recorded with a null verification timestamp and excluded from trusted deduplication. Dedicated-bucket downloads and ingestion require server-recorded generations; file identity edits invalidate trusted checksum metadata. Multipart sessions and file verification metadata are stored in nullable columns on existing tables, and legacy completion is guarded against multipart files. Added a resumable reference client and operator guide.
- Added `resource_mfid` to `TransferOwnershipRead`, `DeletionRequestRead`, and `DeletionPreviewRead`, and to the `deletion_request` field of dataset, sample, instrument, and project detail responses. The values mirror the existing `resource_id` fields, which now document the `_mfid` form as preferred. `DeletionAuditLogRead.resource_id` keeps its current shape.
- Added `dataset_mfid` and `file_mfid` filtering to `GET /ingestion_requests`, accepting the same MFID values as the existing `dataset_id` and `file_id` parameters, which remain as deprecated aliases. Passing a file filter together with a dataset filter selects by file, as before. The single-request path parameter is now documented as `{request_id}` in OpenAPI (URLs unchanged); regenerated OpenAPI operationIds for `GET/PATCH /ingestion_requests/{request_id}` change accordingly. See `docs/remote_file_records.md` for the deferred file-identifier naming.
- Added slug and username resolution to identifier path segments. `{project_ref}` and `{instrument_ref}` accept the MFID or the case-insensitive slug, `{user_ref}` accepts an ORCID, service-account MFID, or username, and `{service_account_ref}` accepts the service-account MFID or username. Applies to every `/instruments/{instrument_ref}/...`, `/users/{user_ref}/...`, `/service_accounts/{service_account_ref}/...`, and `/projects/{project_ref}/users/{user_ref}` route. MFID and ORCID requests behave as before. Each path parameter now documents the forms it accepts in OpenAPI. See `docs/resource_identifier_routes.md`.
- Added `instrument_id` filtering to `GET /datasets` and `GET /datasets/facets`, combinable with a matching `instrument_mfid` (422 if they disagree, 404 for an unknown slug).
- Added `project_mfid` to the `POST /resources/{mfid}/project` body as an alternative or agreeing pair with `project_id`, and `resource_mfid`, `previous_project_mfid`, and `new_project_mfid` to its response.
- Added `dataset_mfid` to thumbnail responses, and `dataset_mfid` and `sample_mfid` to the `POST /datasets/{dataset_mfid}/samples/{sample_mfid}` response.
- `GET /instruments/{instrument_ref}` requested by slug now returns `Content-Location` with the MFID route.
- Added `sort=project_id|title|created|updated` to `GET /projects` and `sort=instrument_id|name|created|updated` to `GET /instruments`, with `direction=asc|desc` and signed cursors bound to the sort. Omitting `sort` keeps each collection's existing order.
- Added the caller's `role` to `GET /projects` and project singleton items (project membership role) and to `GET /instruments` and instrument singleton items (effective role on the instrument record). Previously `role` was always null on these routes.
- Added repeatable `member_ref` and `member_role` filters to `GET /projects` for membership-only listings. `member_ref` accepts any user reference with AND semantics and the same selection rule as `accessible_to_user`; `member_role` restricts the matched roles.
- Added authenticated `POST /print/barcode` for publishing MQTT barcode print jobs and waiting briefly for a print result, moving the MQTT broker credential server-side instead of per-lab-machine `.env` files. Responds 200 with a `status` of `ok`, `error`, or `timeout`, and an optional `detail` noting the requested printer's online status, read from its retained MQTT status heartbeat, when a job doesn't succeed.
- Added `user_capabilities` to user responses, populated for singleton and exact lookups with caller-relative `can_view_private`, `can_edit_profile`, `can_manage_api_key`, and `can_change_platform_role` flags.
- Added the platform-admin directory to broad `GET /users` listings: administrators receive `UserRead` items with email and platform roles across the full directory, while non-administrators keep the group-scoped public-safe listing.
- Added sorting, filtering, and pagination parity to `GET /users`: `sort=name|username|unique_id|email` (email administrator only) with `direction=asc|desc`, `include_total`, signed opaque cursor pagination with `next_cursor` alongside the existing offset, declared `first_name`/`last_name` filters, a `q` substring search with repeatable `search_fields` to choose the searched fields (email administrator only), and repeatable administrator-only `platform_role` filtering.
- Added human platform-role management through `PATCH /users/{user_ref}/platform_role` with `none`, `contributor`, and `admin` values, guarded against service-account callers, self-changes, service-account targets (409 pointing at the service-account route), and demoting the last human platform administrator (409, checked with every human administrator row locked).
- Added administrator-only `DELETE /users/{user_ref}/apikey`, revoking every API key the user holds and invalidating the revoked keys in the principal cache.
- Added non-secret `api_key_status` to the singleton and exact platform-admin view of a user, mirroring the service-account detail response.
- Added the target user's standing `role` in each project's access group to `GET /users/{user_ref}/projects` items.
- Added `can_manage_users` to `GET /account/profile` capabilities, true for platform administrators.
- Added the `GET /projects` collection contract to `GET /users/{user_ref}/projects`: `sort=project_id|title|created|updated` with `direction=asc|desc`, signed cursor pagination with `next_cursor` alongside the existing offset, `include_total`, `include_metadata`, and a repeatable `role` filter on the target user's role. Defaults are unchanged: `project_id` ascending order and an exact `total` unless `include_total=false`. Unknown query parameters now return 422.
- Added permanent deletion of empty projects and instruments through both the reviewed request workflow (admin on the resource may request) and the direct forced path (owner or platform admin). A project or instrument with assigned datasets or samples is rejected with 409 and a structured `blocking` count detail, checked at request creation, at approval with the row locked inside the transaction, and at forced deletion.
- Added a preview-and-confirm flow to `DELETE /resources/{resource_mfid}?force=true` for projects and instruments: without `confirm=true` it returns a `DeletionPreviewRead` describing blockers and what would be removed (group memberships, service-account bindings, join requests, cross-resource grants) and deletes nothing. Datasets and samples keep immediate forced deletion.
- Added a `preview` field to project and instrument deletion-request responses describing what the deletion removes or why it is blocked.
- Added `can_delete` to resource capabilities, true for projects and instruments when the caller may request or force deletion and the resource is empty; false for datasets and samples.
- Project deletion now removes the project access group with its memberships, legacy UserProjectLink rows, join requests, and the group's grants on other resources; instrument deletion removes the operator group with its service-account bindings and grants. Service-account User rows survive.
- Project and instrument deletions now write a `DeletionAuditLog` row on both approval and forced paths and schedule a final GCS record write with the removed-grants summary.
- While a project or instrument has a pending or approved deletion request it is hidden from project and instrument list and search endpoints, and new assignments (datasets, samples, instrument binding, project reassignment, member additions) are rejected with 409.
- Added `deletion_request` fields to project and instrument singleton responses so clients can see an active deletion request, mirroring dataset and sample details.
- Added `include_members=true` to `GET /instruments/{instrument_ref}` and the exact `GET /instruments?instrument_id=` lookup, returning the people and projects holding a role on the instrument record to callers with a role on it and to platform administrators.
- Added `affiliation=maintainer` to `GET /instruments` and `GET /instruments/search` for instruments where the caller holds editor or admin.
- Instrument audit records now include `instrument_id`, `location`, `description`, `instrument_type`, and `status`, and lifecycle status changes write an audit record.
- Added pinned Google Secret Manager references for the Hub client and ORCID credentials, environment-specific deployment settings, and the `HUB_AUTH_ENABLED` activation flag.
- Added `scripts/cleanup_hub_auth.py` (dry-run by default) and hourly Hub authentication cleanup CronJobs for staging and main. Cloud Build deploys them with the API image SHA and preserves their suspend state; both are active. Cleanup error logs are limited to exception types.
- Added concurrent expiry indexes for Hub login transactions, handoff codes, access tokens, and token families.
- Documented Hub logout revocation, rotated-token support, session isolation, and Hub session cleanup requirements in OpenAPI and the login guide.
- Added separate ORCID-backed Hub sign-in, one-use code exchange, short-lived Hub access tokens, and authenticated refresh and revoke routes. Existing API keys remain supported.
- Added authorization-filtered `GET /embeddings` listing with signed cursor pagination, compatible offsets, optional exact totals, and exact ID, resource, chunk-index, and model filters.
- Added a `resource_embedding` table with IVFFlat cosine indexing for storing 768-dimensional, model-labeled text chunks linked to canonical resource MFIDs, plus authenticated `POST /embeddings/search` nearest-neighbor search with resource access filtering.
- Added human-requester deletion history, deterministic cursor pagination, direction and project/resource filters, canonical project MFID snapshots, and public-safe requester and reviewer summaries to deletion audit reads while retaining administrator-wide history, offset pagination, and stable ID fields. Service accounts cannot read deletion audit history.
- Added case-insensitive `resource_name`, `project_id`, `requester_name`, and semantic `status` sorting to deletion-request collections with nulls-last keyset pagination.
- Added nullable public requester and reviewer identity summaries to deletion-request responses while retaining their stable ID fields.
- Added request-time direction and opaque continuation cursors to deletion-request collections while retaining offset pagination and newest-first defaults.
- Added canonical `project_mfid` filtering and response fields to deletion-request collections.
- Added `accessible`, `submitted`, and `reviewable` scopes to `GET /deletion_requests` and caller-specific `can_review` capabilities to deletion-request responses.
- Added optional `Crucible-Privilege-Mode: normal|elevated` selection to authenticated routes, applied-mode and request-ID response headers, account authorization metadata, and structured elevated-request audit logs.
- Added authorization-filtered per-node graph expansion availability for upstream lineage, downstream lineage, and sample-dataset associations.
- Added bounded, authorization-filtered `GET /resources/{resource_mfid}/graph` traversal with typed sample and dataset nodes, typed lineage and association edges, persisted relationship types, deterministic ordering, depth and direction controls, completeness metadata, and online reverse-link indexes.
- Added human-platform-administrator-only `PATCH /service_accounts/{service_account_ref}` for changing a service account's platform role to `none`, `contributor`, or `admin`.
- Added optional `is_service_account` filtering to `GET /users/search` for public-safe person and service-account discovery.
- Added account-level workflow capabilities to `GET /account/profile` and administrator-only `GET /service_accounts` collection and detail routes with platform roles and non-secret API-key status.
- Added authorization-scoped `GET /datasets/facets` and `GET /samples/facets` endpoints with exact stored-value buckets, explicit null buckets, resolved owner/project/instrument references, and opaque bucket pagination. Dataset and sample collections now expose matching field-specific null filters.
- Dataset and sample facet endpoints now return canonical bucket labels and support deterministic raw-value, label, or count ordering through `sort=value|label|count` and `direction=asc|desc`, with stable ties and null buckets last.
- Public-safe user representations now include an `is_service_account` discriminator so clients can distinguish service identities from people without inferring account type from identifier format.
- Dataset, sample, instrument, and project collections and search endpoints now support repeatable `affiliation=owner` filtering based on authoritative owner permissions.
- Dataset, sample, and instrument collections and search endpoints now accept `owner_id` as the preferred exact human-or-service-account filter while retaining their existing owner filters as deprecated aliases.
- Dataset, sample, instrument, and project collections now accept `include_total` to explicitly request or suppress exact authorization-filtered counts. Omitting it preserves each endpoint's existing count behavior.
- Dataset and sample collections now support deterministic `sort=created|updated|name` with `direction=asc|desc`, timezone-aware time filters, and signed opaque continuation cursors.
- Dataset and sample collections now accept `anchor_mfid` to start an inclusive page at a resource within the authorized, filtered, and ordered collection.
- Project and instrument collections now support opaque continuation cursors over their existing deterministic orders while preserving offset pagination and no-cursor behavior.
- Dataset and sample creation now accept either `project_id` or `project_mfid`, or a matching pair. Existing `project_id` requests remain supported with unchanged authorization and error behavior.
- Dataset creation now accepts either `instrument_id` or `instrument_mfid`, or a matching pair. Existing `instrument_id` and legacy `instrument_name` requests remain supported.
- Added `project_id` or `project_mfid` and optional `project_scope=assigned|shared|all` filters to dataset and sample search. Existing `project_id` searches remain assigned-only by default, and filtered results include `project_relation` metadata.
- Added explicit `project_id` or `project_mfid` and optional `project_scope=assigned|shared|all` filters to dataset and sample collections. Matching slug and MFID pairs are accepted, conflicting pairs return 422, and filtered responses include additive `project_relation` metadata.
- Added fixed `image/jpeg` MIME metadata to thumbnail responses and documented the bulk first-thumbnail response schema in OpenAPI.
- Added `PATCH /datasets/{dataset_mfid}/thumbnails/{thumbnail_id}` for authorized thumbnail renaming and image replacement while preserving thumbnail identity and ordering.
- Added thumbnail IDs to bulk first-thumbnail responses so clients can target modification or deletion routes.
- Added a concurrent database index supporting dataset-scoped thumbnail listing, selection, and deletion.
- Added `GET /instruments/{instrument_ref}/service_accounts` for authorized administrators to inspect instrument bindings.
- Added a server warning when an unbound service account successfully creates a dataset for a registered instrument. Enforcement remains deferred.
- Added optional caller-specific `capabilities` to project and instrument responses, including the highest non-owner role the caller may grant. Capabilities are populated for singleton, exact-slug, create, update, and instrument-status responses, and remain null on general collection and search results.
- Added an explicit optional `instrument_mfid` filter to `GET /datasets` for stable instrument-scoped dataset listing.
- Added optional relationship filters: `sample_mfid` on `GET /datasets` and `dataset_mfid` on `GET /samples`. The existing nested relationship routes remain compatible but are deprecated.
- Added caller-specific capabilities to dataset and sample detail responses. Generic resource detail responses now populate capabilities consistently for datasets, samples, projects, and instruments.
- Added nullable `instrument` and `project` identity references to dataset response models. Existing flat `instrument_id`, `instrument_name`, and `project_id` fields remain available for compatibility.
- Added a nullable `project` identity reference to sample response models. The existing flat `project_id` field remains available for compatibility.
- Added `include_datasets=false` to singleton sample and generic resource reads so clients can omit the legacy embedded dataset collection. `include_links` and `GET /datasets?sample_mfid=...` are the preferred relationship interfaces.
- Added `disposition=attachment|inline` to `GET /files/{file_id}/download_link` and `GET /datasets/{dataset_mfid}/download_links`. `attachment` (the default) sets `Content-Disposition: attachment` from the record filename and leaves the stored content type unchanged. `inline` sets `Content-Disposition: inline` with the content type guessed from the filename when recognized, except HTML, XHTML, SVG, XML, and XSL types (including any `+xml` subtype), which are always served as attachments. Any other value returns 422.

### Changed

- Enabled Hub sign-in on production. The API load balancers now accept only the crucible.lbl.gov proxy, which reaches the API over internal load balancers and rate-limits the Hub authentication routes; clients using `https://crucible.lbl.gov/api/v3` are unaffected.
- Production enables signed multipart uploads into `mf-storage-prod` (`MULTIPART_UPLOAD_ENABLED`, `MULTIPART_UPLOAD_BUCKET`, `MULTIPART_UPLOAD_SIGNER` = `crucible-mpu-signer-prod`, `MULTIPART_UPLOAD_PROJECT`). See `docs/archive/release_2026-10_staging_to_main.md`.
- Multipart upload initiation with an existing filename now returns a structured 409 `{"code": "filename_exists", "file_mfid", "etag", "message": "Filename already exists"}` instead of a plain string, so clients can offer a replace.
- Uploads now go to one bucket per environment, set by `UPLOAD_BUCKET` (default `mf-storage-prod`): legacy and multipart uploads share it, and `MULTIPART_UPLOAD_BUCKET` must equal it when multipart is enabled. Staging now writes to `mf-storage-staging` and keeps reading existing records from `mf-storage-prod` through the new optional `STORAGE_READ_BUCKETS`. Production configuration is unchanged. Ingestion consumers must mount and resolve the environment bucket.
- Platform contributors and administrators, human or service account, may create datasets and samples in any project, owned by themselves or by any registered identity, without project membership (`POST /datasets`, `POST /samples`). Other callers may create only records they own and still need contributor-or-above standing in the target project; that 403 now names the caller (`Caller '<id>' must be contributor or above in project '<project>' to create there`). A non-contributor naming another owner now gets 403 `Only platform contributors and administrators may create a dataset/sample on behalf of another user` whether or not a project is supplied, instead of 400 without a project.
- Download links now set `Content-Disposition` from the record filename and default to `attachment`, keeping legacy downloads unchanged and making multipart downloads save with the record name instead of a guessed type. The previous `inline`-by-default behavior with filename-guessed content type is available by passing `?disposition=inline`.
- New multipart upload objects are stored at `{dataset_mfid}/_files/{upload_id}` in the multipart bucket instead of `uploads/{dataset_mfid}/{upload_id}/content`. Legacy upload routes reject filenames whose first segment is `_files` with 400, and the legacy parallel upload token can no longer reach `{dataset_mfid}/_files/`.
- Renamed OpenAPI path parameters to match what they accept; request URLs are unchanged: `{dsid}`, `{dataset_id}`, `{parent_dataset_id}`, and `{child_dataset_id}` to `_mfid` names; `{sample_id}`, `{parent_sample_id}`, and `{child_sample_id}` to `_mfid` names; `{resource_id}` and `{unique_id}` under `/resources` to `{mfid}`; `{entity_id}` to `{entity_mfid}`; `{instrument_mfid}` to `{instrument_ref}`; `{user_id}` and `{orcid}` under `/users` and project membership to `{user_ref}`; `{unique_id}` and `{sa_unique_id}` for service accounts to `{service_account_ref}`; and `/project_graph/{project_id}` to `{project_ref}`. Clients generated from OpenAPI see new argument names.
- `PUT` and `DELETE /resources/{mfid}/access/projects/{project_ref}` now accept a project MFID. They previously accepted only the slug and returned 404 for an MFID.
- `POST /access_groups/{group_name}/join` and the `group_name` filter on `GET /join_requests` now accept a project MFID, and join-request responses return the project's stored slug.
- `POST /service_accounts` now rejects a supplied `unique_id` that is not a 26-character MFID (422), or that is already used by a user, resource, or access group (409). Previously any unused string was accepted and only other users were checked.
- An unknown `instrument_id` in `POST /datasets` or `PUT /datasets/{dataset_mfid}/instrument` now returns 404, matching an unknown `instrument_mfid`. It previously returned 400. An unregistered `instrument_name` still returns 400.
- `POST /resources/{mfid}/project` now stores and returns the target project's slug with its stored casing.
- `UserRead` now exposes `platform_role` to the user themselves and to platform administrators.
- `GET /users/{user_ref}/projects` is now available to callers with `platform_role` `contributor` or `admin`, including unbound service accounts, matching the authority for `accessible_to_user`. Instrument-group membership no longer grants access, and the reduced `ProjectMinimalRead` projection for instrument-group members is removed; every authorized caller receives `ProjectWithLead`.
- Project slugs are now matched case-insensitively in `GET /projects?project_id=`, the `GET /projects/{project_id}` compatibility alias, project filters, and project references in other routes, matching instrument slugs. Creating or renaming a project to a slug that differs from another only by case returns 409. Migration `b7e2d9c4a1f3` adds case-insensitive unique indexes on `project.project_id` and `instrument.instrument_id`; `migrations/data/20261005_audit_case_insensitive_slugs.py` audits for conflicting slugs first.
- Dataset responses now resolve `instrument_name` from the assigned instrument through `instrument_mfid`, so instrument renames are reflected immediately. Datasets without a registered instrument keep their stored label. Generic dataset PATCH accepts either the stored label or the current name as the unchanged value.
- Deprecated the `instrument_name` lookup on `POST /datasets` and the `instrument_name` filter on `GET /datasets`. The filter now matches the displayed name. Use `instrument_id` or `instrument_mfid`.
- `project_id` no longer has to differ from instrument display names.
- Instrument creation no longer grants the instrument operator group `editor` on the instrument record. `migrations/data/20261005_remove_instrument_group_record_grants.py` removes the grant from existing instruments.
- Deprecated `POST /users/{user_ref}/access_groups/{group_name}` now rejects human users for instrument groups with 409. Instrument groups hold service accounts only.
- Production readiness now expects database revision `f3b7c2a91d04`.
- `GET /ingestion_requests` now returns the authorization-filtered collection for any caller with viewer access to the parent datasets; non-administrators no longer need to supply a dataset or file filter.
- Deletion-request responses now require caller capabilities, statuses and review decisions use documented enums, and collection ordering uses descending request time and request ID.
- Authenticated responses now use private caching, and elevated responses use `Cache-Control: private, no-store`. Omitted privilege mode preserves existing platform-administrator behavior.
- Added `platform_role=contributor` for platform-wide read and create access without administrator authority. New service accounts receive this role by default and may use `accessible_to_user` for project, sample, and dataset collections. `is_service_account` remains an identity discriminator, and non-admin service accounts are capped at contributor even when an ACL or group standing would otherwise grant a higher role.
- Removed legacy group-based administrator and resource-permission fallbacks. Platform administration now requires `platform_role=admin`, and project and instrument authorization now requires canonical resource ACLs.
- Project membership routes now reject editor, admin, and owner roles for non-admin service accounts instead of storing roles that their authorization ceiling cannot use.
- API responses containing API keys now send `Cache-Control: no-store` on account, administrator, service-account creation, and key-rotation routes.
- Initial metadata creation, associated-file creation, upload initiation and completion, keyword association, ingestion-request creation, and same-project resource linking now require contributor access. Repeated associated-file creation returns the existing record without updating it.
- Associated-file creation now accepts `storage_path` for non-GCS catalog-only files so their location can be registered atomically.
- Dataset, sample, instrument, and project collection, search, and singleton queries now reject unknown parameters with 422. The generic resource singleton follows the same rule. Previously working exact dataset, sample, and instrument filters remain explicit and deprecated where appropriate.
- Dataset and sample pagination preserves the legacy behavior in which cursor takes precedence when cursor and offset are supplied together.
- Signed continuation cursors now use a compact internal Protobuf payload containing only page position. Request filters, ordering, resource type, and caller identity remain bound through the HMAC signature without being exposed in the token.
- API-owned resource creation and modification timestamps now use timezone-aware database columns, canonical UTC `Z` responses, database-generated values, immutable creation times, and automatically advanced modification times. Migration revision `8c4f2a1d7e60` converts approved legacy formats using the documented Pacific-time policy, converts empty and `NaN` sentinels to null, and rejects unsupported values. Revision `3f2b6a9c1d84` adds database enforcement. The producer-supplied dataset `timestamp` field is unchanged.
- Deployment readiness now reports degraded status when the connected database revision differs from `EXPECTED_DB_REVISIONS`.
- Trigram searches now normalize bounded input, treat prefix wildcard characters literally, bind the request-local similarity threshold, and use canonical unique IDs as deterministic relevance tie-breakers.
- Dataset, sample, instrument, and generic resource singleton GETs now resolve the public-safe owner object by default. Pass `include_owner=false` to omit it. Collection and search expansion remains opt-in.
- Large bulk first-thumbnail lookups now emit a structured operator warning above a configurable threshold without rejecting the request, and duplicate identifiers are removed before querying.
- Thumbnail uploads now release their database connection during image processing and recheck edit authorization before writing.
- Concurrent thumbnail uploads to the same dataset now serialize their dataset-scoped idempotency check, preventing identical requests from creating duplicate rows.
- Deletion-request and join-request collections now resolve project information in their page queries instead of issuing per-item queries. File detail responses reuse the already authorized dataset instead of lazily loading it again.
- Project membership now follows a strict hierarchy: editors manage contributor and viewer roles, admins manage editor and below, and owners manage admin and below. Project capabilities report the corresponding lower `max_grant_role`.
- Canonical and generic resource detail reads now share service loaders. `GET /resources/{mfid}` mirrors canonical authorization semantics and avoids unnecessary deletion-request work for projects and instruments.
- Instrument display names now preserve caller-supplied casing instead of being forced to lowercase.
- Instrument collection filters and dataset assignment by `instrument_id` now resolve case-insensitively.
- Instrument search accepts an optional `status` filter with `active`, `maintenance`, and `decommissioned` values. Omitting it preserves the existing all-status search behavior.
- Project membership mutation routes now accept named roles (`viewer`, `contributor`, `editor`, `admin`, and `owner`) instead of integer enum values. `owner` remains rejected with 409 and requires the ownership-transfer route.
- Service accounts may no longer register instruments, including when they hold platform-admin status. Human platform administrators may continue registering an instrument for another identity.

### Deprecated

- Reading an existing API key through `GET /account/apikey` and `POST /users/{user_ref}/apikey`. Both still work for API-key callers and return `Deprecation: @1791763200`; use the regenerate routes, which keep working once keys are stored hashed.
- The `email` and `username` query parameters on `POST` and `DELETE /projects/{project_ref}/users/{user_ref}`. Put the username in the path instead.
- Email in `{user_ref}` path segments. It still resolves, but emails in URLs are written to access logs; use the ORCID, MFID, or username.
- `GET /users/by-username/{username}`. Use `GET /users/{user_ref}`.
- `ThumbnailRead.dataset_id` (internal integer key) in favor of `dataset_mfid`, and `dataset_id` and `sample_id` on the dataset-sample link response in favor of `dataset_mfid` and `sample_mfid`.
- The `resource_id` fields on `TransferOwnershipRead`, `DeletionRequestRead`, and `DeletionPreviewRead`, and on dataset, sample, instrument, and project detail `deletion_request` objects, in favor of the new `resource_mfid` twin. Both fields are returned with identical values.
- The `dataset_id` and `file_id` filters on `GET /ingestion_requests` in favor of `dataset_mfid` and `file_mfid`. Both accept the same MFID values.
- Undeclared `GET /users` query parameters are now ignored instead of being applied as user-attribute filters. Affected responses carry `Deprecation: @1799280000` and name the ignored parameters in the server log; the parameters will be rejected with 422 at that announced date. No `Sunset` header is sent.

### Security

- GCS file downloads (`GET /files/{file_id}/download_link`, `GET /datasets/{dataset_mfid}/download_links`) and ingestion requests now serve only objects in an allowed bucket (`UPLOAD_BUCKET`, `MULTIPART_UPLOAD_BUCKET`, or `STORAGE_READ_BUCKETS`) under the file's own dataset (`{dataset_mfid}/...`). Previously a caller with EDIT on a dataset could point a file record's `storage_path` at any object or bucket in the project through `PATCH /files/{file_id}` and download it. A record outside the rule returns 409 with `{"detail": {"code": "storage_location_invalid", "reason": ..., "message": "File storage location is not valid: <reason>"}}` and is `null` in bulk links. A read-only audit (`migrations/data/20261013_audit_file_storage_paths.py`) found no production record falls outside the rule; the 229 staging test records under the retired `uploads/` multipart layout now return 409. A multipart object without a recorded generation now returns 409 instead of 403 on download and ingestion.
- `PATCH /files/{file_id}` returns 422 when it would set a GCS file's `storage_path`, switch a record with a path to the `gcs` backend, or change a GCS file's `size` or `sha256_hash`. Echoing current values, clearing the path, and all edits to non-GCS catalog records are unchanged.
- The access token returned by `POST /datasets/{dataset_mfid}/upload/initiate/multipart` is now downscoped with a Credential Access Boundary to objects under `{dataset_mfid}/` in the upload bucket (`roles/storage.objectAdmin` within that prefix). Parallel uploads, overwrites of the dataset's own files, and reading them back keep working; the token can no longer read, write, delete, or list objects of other datasets or multipart objects under `{dataset_mfid}/_files/`. The response shape is unchanged. Minting now calls Google STS, so the API needs egress to `sts.googleapis.com`; if the exchange fails the route returns 503 and never falls back to the broad token. Setting `LEGACY_UPLOAD_TOKEN_DOWNSCOPE=false` restores the previous service-account token as an emergency rollback.
- Hub sessions can no longer read existing API keys: `GET /account/apikey` returns 403 `api_key_read_not_allowed`, and `POST /users/{user_ref}/apikey` returns 409 `api_key_exists` instead of the existing key.
- Creating an API key from a Hub session (regenerate routes, `POST /users/{user_ref}/apikey`, `POST /service_accounts`, `POST /service_accounts/{service_account_ref}/rotate_key`) requires an ORCID sign-in within the last 600 seconds, otherwise the API returns the RFC 9470 step-up challenge (401, `WWW-Authenticate: Bearer error="insufficient_user_authentication"`). API-key callers are unaffected.
- Hub sessions default to normal privilege mode when `Crucible-Privilege-Mode` is omitted; API keys keep the elevated compatibility default for administrators.
- `HUB_AUTH_ENABLED` now defaults to `false` when unset.
- Hub authentication logs redact validation failures and unexpected errors, and every Hub authentication response sends `Cache-Control: no-store`.
- `HUB_AUTH_CALLBACK_URI` accepts a comma-separated list of exact Hub callbacks; every entry must be HTTPS or an HTTP loopback address. Staging now enables Hub sign-in in its checked-in configuration with the local development callback `http://localhost:3000/hub/auth/callback`.
- User email addresses are now unique, compared case-insensitively after trimming whitespace (migration `e5a1c8d93f27`, after auditing existing data with `migrations/data/20261012_audit_user_emails.py`). `PATCH /account/profile`, `PATCH /users/{user_ref}`, and `POST /users` return 409 without identifying the other account, so an address can no longer be claimed by a second account and redirect email-based member additions or access grants. Blank emails are stored as null. Emails remain optional and unverified.
- Username and email conflicts now return a structured 409 detail, `{"code": "already_in_use", "fields": [...], "message": ...}`, listing every conflicting field, instead of the plain string `Username already taken`. Clients that print `detail` still show the message.
- Non-administrators can no longer filter users by `platform_role`; the filter is administrator-only and other callers receive 403.
- Removed the generic `GET /users` loop that turned any undeclared query parameter matching a `User` attribute into a filter, which let callers probe arbitrary columns including internal ones.

### Fixed

- `GET /users/{user_ref}/projects` now hides projects with a pending or approved deletion request, matching `GET /projects`.
- Legacy upload initiation and `POST /datasets/{dataset_mfid}/files` no longer deduplicate against multipart files whose SHA-256 is an unverified client claim, and no longer return 500 when several files share a hash. Both, and legacy upload completion, now return 409 for a filename reserved by an active multipart upload instead of making that upload fail at publication.
- Part URLs within a multipart signing batch are signed concurrently.
- `POST /datasets` with an `instrument_name` shared by more than one instrument now returns 409 instead of a server error.
- Permanent dataset and sample deletion now removes associated resource embeddings instead of failing with a foreign-key violation.
- Escaped profile values in the legacy API-key page and removed dynamic values from its inline JavaScript.
- Disabled raw Uvicorn access logs while retaining API request logs without query strings.
- Deletion-request submitters can now read their own request detail without receiving deletion-review authority.
- Identical thumbnail images uploaded to different datasets now remain independently owned instead of moving an existing thumbnail between datasets.
- Thumbnail uploads now verify dataset edit access before decoding or processing image data.
- Thumbnail deletion now applies dataset edit authorization in its initial lookup, consistently returning 404 for absent or inaccessible datasets and avoiding a redundant permission query.
- Thumbnail uploads now strictly validate base64 input and return stable errors without exposing image-library exception details.
- Thumbnail normalization now enforces configurable 50 MiB and 50 megapixel source limits, applies EXIF orientation, uses the first animation frame, composites transparency onto white, and accepts common raster formats only.
- Dataset thumbnail lists are now ordered by thumbnail ID, matching the bulk endpoint's definition of the first thumbnail.
- Join-request review now locks the pending request during its state transition, and the database prevents concurrent duplicate pending requests for the same user and access group.
- Project results from `GET /resources/metadata/search` now resolve titles through the canonical project MFID instead of the legacy project slug.
- Instrument updates now reject explicit null values for required identifiers, names, and locations with 422 instead of failing during persistence.
- Repeating a project add-member request with the member's current role remains idempotent, while requesting a different role now returns 409 and directs the caller to the role-update route instead of silently ignoring the requested change.
