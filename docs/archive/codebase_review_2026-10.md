# Crucible API Codebase, Query, and Security Review

Date: 2026-10-15

## Scope

This document records the findings of a comprehensive static audit of `crucible-api` across four areas:

- Concrete bugs, data corruption hazards, and security/authorization vulnerabilities in routes, services, and utilities.
- Database query performance, pagination, facet aggregation, indexing, and ORM/Alembic schema alignment.
- FastAPI middleware, concurrency, caching, and synchronous vs asynchronous execution patterns.
- Kubernetes manifests, Docker packaging, dependency hygiene, and repository tooling.

## Architectural Strengths

- **Unified Resource and RBAC Model**: The core [`Resource`](../../app/models/core_models.py), [`AccessGroup`](../../app/models/core_models.py), and [`ResourceACL`](../../app/models/core_models.py) schema paired with centralized authorization helpers in [`app/utils/permissions.py`](../../app/utils/permissions.py) (`load_principal`, `require_permission`, `permission_filter`) provides a composable RBAC foundation that avoids row-multiplying authorization joins via correlated `EXISTS` subqueries.
- **Keyset Pagination and Search**: Deterministic keyset pagination with HMAC-SHA256-signed cursors in [`app/utils/cursor_utils.py`](../../app/utils/cursor_utils.py) and PostgreSQL trigram plus full-text search ranking in [`app/utils/search_utils.py`](../../app/utils/search_utils.py) are well-suited for high-volume dataset and sample collections.
- **Migration Discipline**: [`migrations/env.py`](../../migrations/env.py) enforces a single linear Alembic revision chain and guards against accidental production execution via `ALLOW_PRODUCTION_MIGRATION`.

---

## 1. Concrete Bugs, Data Corruption Hazards, and Security Vulnerabilities

### 1.1 Dataset Deletion Destroys Shared GCS Blobs Across Deduplicated Datasets

- **Locations**: `app/utils/deletion_utils.py:176-203`, `app/services/multipart.py:174-182`, `app/api/routes/gcs_storage.py:162-171`, `app/utils/gcs_record_utils.py:67-111`
- **Problem**: Both `MultipartUploadService.reserve` (`app/services/multipart.py:174-182`) and `_validate_and_prepare_upload` (`app/api/routes/gcs_storage.py:162-171`) deduplicate uploaded files by `sha256_hash`, creating new `AssociatedFile` rows that point to the existing `storage_path` in `mf-storage-prod`. When a dataset is deleted, `_delete_gcs_blobs_for_dataset` (`app/utils/deletion_utils.py:189-197`) unconditionally deletes every `af.storage_path` from GCS without checking whether other `AssociatedFile` rows (on other datasets or within the same dataset) still reference the same `storage_path`. Deleting one dataset permanently destroys the underlying GCS objects for all other datasets that deduplicated against it.
- **Missing Record Cleanup**: Dataset deletion never deletes `{dsid}/record.json` and `{dsid}/metadata.json` from the `mf-record-prod` bucket written by `write_dataset_records_to_gcs` (`app/utils/gcs_record_utils.py:67-111`).
- **Recommendation**: Before deleting a GCS blob in `_delete_gcs_blobs_for_dataset` (and in file replacement retirement), check `SELECT EXISTS(SELECT 1 FROM associatedfile WHERE storage_path = :path AND id != :deleted_id)`. Also delete `{dsid}/record.json` and `{dsid}/metadata.json` from the record bucket during dataset deletion.

### 1.2 Project Reassignment Destroys Shared Project ACL Grants

- **Locations**: `app/api/routes/resources.py:810-854, 1123-1194`
- **Problem**: Resources can be shared with additional projects via `PUT /resources/{mfid}/access/projects/{project}` (`set_resource_access`, `app/api/routes/resources.py:810-854`), creating additional `ResourceACL` rows whose `AccessGroup.group_type == AccessGroupType.PROJECT.value`. However, `_current_project_grants` (`app/api/routes/resources.py:1123-1132`) fetches all project-typed `ResourceACL` rows on `mfid` without filtering to `AccessGroup.group_name == entity.project_mfid`. In `set_resource_project` (`app/api/routes/resources.py:1135-1194`), preview mode (`confirm=False`, line 1159) reports an arbitrary `current_grants[0]`, and confirm mode (`confirm=True`, lines 1178-1179) deletes every project `ResourceACL` on the resource, wiping out all cross-project sharing grants instead of only replacing the previous owning project's grant.
- **Recommendation**: Filter `_current_project_grants` (or partition `current_grants` in `set_resource_project`) so only the `ResourceACL` row matching the previous owning `entity.project_mfid` is removed when reassigning the owning project.

### 1.3 Cross-Dataset SHA-256 Deduplication Leak, Overbroad GCS Upload Token, and Resumable Upload Completion Flaws

- **Locations**: `app/api/routes/gcs_storage.py:96-135, 162-171, 294-356`, `app/models/core_models.py:549-561`
- **Cross-Dataset Hash Leak**: `_validate_and_prepare_upload` (`app/api/routes/gcs_storage.py:162-171`) queries `select(AssociatedFile).where(AssociatedFile.sha256_hash == body.sha256_hash)` globally without checking `Permission.READ` on the source dataset or verifying `AssociatedFile.size == body.size` (both of which `MultipartUploadService.reserve` enforces at `app/services/multipart.py:175-182`). A caller who knows a SHA-256 hash can link another project's private GCS blob into their own dataset and download it.
- **Overbroad Downscoped Token**: `_mint_upload_token` (`app/api/routes/gcs_storage.py:111-118`) grants `roles/storage.objectAdmin` on `projects/_/buckets/{bucket}/objects/{dsid}/*` to callers with only `Permission.CREATE` (`Role.CONTRIBUTOR`), allowing a contributor to overwrite or delete existing objects under `{dsid}/` in GCS without `Permission.EDIT` or `Permission.DELETE`.
- **Resumable Upload Completion Bugs**: `complete_resumable_upload` (`app/api/routes/gcs_storage.py:294-356`) compares `session_record.expires_at < datetime.now(timezone.utc)` at line 319 (`UploadSession.expires_at` is typed `str` in `app/models/core_models.py:557`, raising `TypeError` when returned as `str` or naive `datetime`), does not lock the `UploadSession` row with `with_for_update()`, does not check for duplicate `(dataset_id, filename)`, does not verify blob size or CRC32C against GCS, and omits setting `storage_generation` and calling `touch_resource_modification_time`.
- **Recommendation**: Restrict SHA-256 deduplication in `gcs_storage.py` to the same dataset (or datasets where the caller has `Permission.READ` and matching `size`), scope the CAB rule to the exact target object path (`objects/{dsid}/{filename}`) with `roles/storage.objectCreator` unless replacing, and align `complete_resumable_upload` with `MultipartUploadService.complete`.

### 1.4 Cross-Filename SHA-256 Collision Hijack, Suffix Matching False Positives, and Missing Modification Timestamp Touch in Multipart Service

- **Locations**: `app/services/multipart.py:173-189, 387-405, 444-452`, `app/api/routes/gcs_storage.py:69-79`
- **Cross-Filename SHA-256 Hijack**: In `MultipartUploadService.reserve` (`app/services/multipart.py:174-189`), same-dataset `sha256_hash` deduplication runs before the `filename` check. If `dsid/file_a.txt` already exists and a user uploads `dsid/file_b.txt` with identical content, `reserve` immediately returns `state="completed"` pointing to `dsid/file_a.txt` without creating an `AssociatedFile` record for `dsid/file_b.txt`. Furthermore, if `dsid/file_b.txt` already exists with different content and `replace=False`, uploading content matching `dsid/file_a.txt` under the name `file_b.txt` bypasses the `409 filename_exists` check.
- **Missing Modification Touch on New File**: In `MultipartUploadService.complete` (`app/services/multipart.py:387-405`), `touch_resource_modification_time(session, dsid)` is called when replacing an existing file (line 399) but omitted when creating a new file (lines 400-402). New multipart file uploads neither bump `Resource.modification_time` nor trigger `_record_record_activity` (`app/services/activity.py:111-143`) to update `Project.last_activity_at` and `Instrument.last_activity_at`.
- **Suffix Matching False Positives**: Both `_get_existing_file_by_name` (`app/api/routes/gcs_storage.py:77`) and `MultipartUploadService._find_associated_file_by_name` (`app/services/multipart.py:450`) check `af.filename.endswith(basename)` without a leading `/` boundary, causing `bar.tif` to falsely match `dsid/foobar.tif`.
- **Recommendation**: Check `_find_associated_file_by_name` before hash deduplication in `reserve`, call `touch_resource_modification_time(session, dsid)` on both new and replaced files in `complete`, and match `af.filename == canonical_name or af.filename.endswith("/" + basename)`.

### 1.5 Missing Authorization on Dataset Processing Endpoints and Broken RabbitMQ Publishing

- **Locations**: `app/api/routes/datasets.py:139-211`, `app/api/routes/files.py:31-39, 201-249`, `app/utils/rmq_utils.py:17-41`
- **Missing Authorization**: `request_carrier_segmentation` (`POST /datasets/{dsid}/carrier_segmentation`, `app/api/routes/datasets.py:139-153`) and `request_insitu_aggregation` (`POST /datasets/{dsid}/insitu_aggregation`, `app/api/routes/datasets.py:155-169`) do not check dataset existence, `deletion_filter_for(Dataset)`, or `require_permission`. `request_rga_analysis` (`app/api/routes/datasets.py:171-190`) and `request_mosaic_stitch` (`app/api/routes/datasets.py:192-211`) check dataset existence in the database but never call `require_permission` or `deletion_filter_for(Dataset)`.
- **Shadowed RabbitMQ Password**: In `app/utils/rmq_utils.py:17-22`, `rmq_pw = get_secret("RABBITMQ_DEFAULT_PW", ...)` is fetched at module import time (line 17), but `def send_pika_message(message, exchange, routing_key, user = None, rmq_pw = '')` (line 19) shadows the module-level `rmq_pw` with `''` whenever callers omit `rmq_pw` (all callers in `datasets.py` and `files.py` omit `rmq_pw`).
- **Premature Commit and Unvalidated Size/Env in File Ingestion**: In `create_file_ingestion_request` (`app/api/routes/files.py:201-249`), the `IngestionRequest` row is committed (lines 217-218) before calling `get_ingestion_tier(af.size)` (`af.size` is `Optional[int]`, raising `TypeError` when `None`) and before `send_pika_message` (line 245), leaving orphaned `status='requested'` rows on failure. Line 235 also reads `os.getenv('RMQ_ROUTING_SUFFIX')` with no default, producing `'ingestion-None-small'` when unset.
- **Recommendation**: Require `Permission.EDIT` (or `Permission.CREATE`) and `deletion_filter_for(Dataset)` on all four dataset analysis endpoints, default `rmq_pw: Optional[str] = None` in `send_pika_message` and resolve the secret lazily when `None`, and validate `af.size` and publish (or roll back on failure) in `create_file_ingestion_request`.

### 1.6 Lineage and Graph Authorization Bypass, Soft-Delete Traversal, Unassigned-Project Full Table Scan, and Cycle/Self-Link Hazards

- **Locations**: `app/api/routes/projects.py:674-705`, `app/api/routes/entities.py:42-194, 228-303, 473-496`, `app/api/routes/datasets.py:927-978, 1674, 1706-1779`, `app/api/routes/samples.py:80-130, 834, 866-936`
- **Missing Project Permission Check**: `project_dataset_graph` (`GET /projects/{project_id}/dataset-graph`, `app/api/routes/projects.py:674-705`) never calls `require_permission(session, auth_res, proj.unique_id, Permission.READ)` on the project (unlike `project_sample_graph` at line 653 and `project_entity_graph` at line 720).
- **Unauthorized/Deleted Stub Nodes in `entity_graph`**: `entity_graph` (`app/api/routes/entities.py:67-109`) eager-loads `Sample.parents` and `Dataset.parents` without permission or deletion filters and adds `(parent.unique_id, child.unique_id)` to the NetworkX graph `G`, leaking unauthorized or pending-deletion parent MFIDs as stub nodes in the response.
- **Unfiltered Intermediate Traversal in BFS and Recursive CTEs**: `entity_graph_bfs` (`app/api/routes/entities.py:228-303`), `dataset_graph_cte` (`app/api/routes/datasets.py:1706-1779`), and `sample_graph_cte` (`app/api/routes/samples.py:866-936`) traverse `SampleLink` and `DatasetLink` edges before applying `permission_filter` and `deletion_filter_for`, allowing traversal across unauthorized or soft-deleted intermediate nodes. `entity_graph_cte` (`app/api/routes/entities.py:473-496`) also omits `relationship_type` on edges.
- **`NULL` Project Full Table Scan**: When a target dataset or sample has `project_mfid = None`, the legacy graph handlers (`app/api/routes/datasets.py:1674`, `app/api/routes/samples.py:834`, `app/api/routes/entities.py:71-80`) execute `WHERE project_mfid IS NULL`, loading every unassigned dataset and sample in the database into memory.
- **Missing Self-Link and Cycle Validation**: `link_dataset_parent_child` (`app/api/routes/datasets.py:927-978`) and `link_sample_parent_child` (`app/api/routes/samples.py:80-130`) do not reject self-links (`parent_id == child_id`) or cycles.
- **Recommendation**: Enforce `require_permission` on `project_dataset_graph`, filter traversal steps by `permission_filter` and `deletion_filter_for`, guard against `project_mfid is None` before running project-scoped queries, and reject `parent_id == child_id` and cycles when linking datasets or samples.

### 1.7 SSRF and Unhandled Index/Attribute Errors in `/extract_metadata`

- **Locations**: `app/api/routes/ai.py:35-96`
- **Problem**: `extract_metadata` (`app/api/routes/ai.py:49-52`) allows any authenticated caller to supply an arbitrary `body.api_url` and `body.api_key` (`anthropic.Anthropic(api_key=body.api_key, base_url=base_url)`), causing the API server pod to issue outbound HTTP requests to arbitrary internal cluster or cloud metadata URLs and reflecting exception strings in the 503 response (`detail=f"AI service error: {e}"` at line 79). In addition, line 82 (`raw = response.content[0].text.strip()`) sits outside the `try` block and raises an unhandled `IndexError` or `AttributeError` (HTTP 500) if `response.content` is empty or contains non-text blocks.
- **Recommendation**: Remove caller-controlled `api_url` (or restrict it to an explicit allowlist of trusted LLM gateway hosts), sanitize upstream error messages, and guard `response.content` extraction inside the `try` block.

### 1.8 Staging Overwrites Production GCS Record Bucket

- **Locations**: `app/utils/gcs_record_utils.py:9`
- **Problem**: `app/utils/gcs_record_utils.py:9` hardcodes `_RECORD_BUCKET = "mf-record-prod"`. Dataset creation and updates in staging write `{dsid}/record.json` and `{dsid}/metadata.json` directly into the production `mf-record-prod` bucket.
- **Recommendation**: Read `_RECORD_BUCKET` from an environment variable (such as `GCS_RECORD_BUCKET`) configured per environment in `k8s-conf/configmap-*.yaml`.

### 1.9 Inconsistent Soft-Delete (`deletion_filter_for`) Enforcement Across List, Facet, Detail, and Sub-Resource Endpoints

- **Locations**: `app/api/routes/datasets.py:635-642`, `app/api/routes/samples.py:394-401, 694-698, 751-754`, `app/services/resource_facets.py:133-140, 147-153, 308-315, 384-391`, `app/api/routes/users.py:809-813`
- **Problem**:
  - `GET /samples?dataset_mfid=...` (`app/api/routes/samples.py:394-401`) and `GET /samples/facets?dataset_mfid=...` (`app/services/resource_facets.py:147-151, 384-391`) resolve `Dataset.id` without `.where(deletion_filter_for(Dataset))`.
  - `GET /datasets/facets?sample_mfid=...` (`app/services/resource_facets.py:133-138, 308-315`) resolves `Sample.id` without `.where(deletion_filter_for(Sample))`.
  - `GET /samples/{sample_id}/datasets` (`app/api/routes/samples.py:694`), `GET /samples/{child_sample_mfid}/parents` (`app/api/routes/samples.py:751`), and `GET /users/{user_ref}/datasets/{dataset_mfid}` (`app/api/routes/users.py:809-811`) omit `deletion_filter_for` on the target resource.
  - Direct detail endpoints (`GET /datasets/{dsid}`, `GET /samples/{spid}`, `GET /projects/{ref}`, `GET /instruments/{ref}`, `GET /resources/{mfid}`) and sub-resource reads (`GET /datasets/{dsid}/associated_files`, `/thumbnails`, `/keywords`) return resources with active `pending` or `approved` `DeletionRequest` rows, whereas `GET /datasets/{dsid}/download-url` (`app/api/routes/gcs_storage.py:474`) returns 404.
- **Recommendation**: Apply `deletion_filter_for` consistently across all linked-resource filter resolutions and standardize the detail/sub-resource visibility contract for resources with active deletion requests.

### 1.10 API Key Verification, Expiration, and Legacy OAuth Bugs

- **Locations**: `app/api/routes/account.py:157-170, 185-212`, `app/api/routes/users.py:560-568`, `app/utils/auth_utils.py:96-116, 129, 156-162`, `app/api/routes/auth.py:43-87, 105-107, 271-272, 297-300`
- **Expired API Keys Treated as Valid on Account Endpoints**: `verify_account_apikey` (`app/api/routes/account.py:185-212`) does not filter `ApiToken.token_created > one_year_ago` in SQL, computes `expires_dt` at line 206, and hardcodes `valid=True` at line 208 without checking if `expires_dt < datetime.now(timezone.utc)`. `get_account_apikey` (`app/api/routes/account.py:157-170`) likewise returns expired tokens.
- **Incompatible Timestamp Parsing**: `app/api/routes/account.py:201-209` and `app/api/routes/users.py:560-568` parse `token.token_created` with `datetime.strptime(created_at, "%Y-%m-%d")`, whereas `verify_api_key` (`app/utils/auth_utils.py:129`) uses `datetime.fromisoformat()`. Any ISO timestamp with a time component raises `ValueError` and produces `expires_at=None`.
- **`MultipleResultsFound` After Key Expiry**: `find_or_add_api_access` (`app/utils/auth_utils.py:96-116`) does not delete expired `ApiToken` rows before inserting a new token and calls `.one_or_none()` on `ApiToken.user_unique_id` (which has no unique constraint), causing `MultipleResultsFound` (HTTP 500) once a user has both an expired and a new token row.
- **Unhandled `RedirectResponse` in `decode_token` Callers**: `decode_token` (`app/utils/auth_utils.py:156-162`) returns a `RedirectResponse` instance on `JWTError`, which `get_apikey` (`app/api/routes/auth.py:105-107`) and `regenerate_apikey` (`app/api/routes/auth.py:271-272`) immediately index (`user_info["orcid"]`), raising `TypeError: 'RedirectResponse' object is not subscriptable` (HTTP 500).
- **Legacy OAuth Flow Vulnerabilities**: `redirect_exchange` (`app/api/routes/auth.py:43-87`) checks `if next_page == "None":` when `request.cookies.get("after_login_return_to_page")` returns Python `None`, passing `None` to `RedirectResponse(None)` (HTTP 500), allows open redirects when the cookie contains an external URL, uses a static `nonce=whatever` without OAuth `state` validation, and calls `requests.get`/`requests.post` without timeouts. `GET /regenerate_apikey` (`app/api/routes/auth.py:297-300`) mutates state on a `GET` request authenticated via cookie.

### 1.11 Service Account Guard Bypass and Missing `UserProjectLink` on Project Ownership Transfer

- **Locations**: `app/api/routes/resources.py:947-984`, `app/api/routes/projects.py:58-67`
- **Problem**: `_transfer_project_ownership` (`POST /resources/{mfid}/ownership`, `app/api/routes/resources.py:947-984`) transfers `Role.OWNER` on a project without calling `_forbid_service_account_above_contributor` (`app/api/routes/projects.py:58-67`), allowing a service account to become project `OWNER`, and omits creating the legacy `UserProjectLink` row.
- **Recommendation**: Call `_forbid_service_account_above_contributor(target_user, Role.OWNER)` and maintain `UserProjectLink` parity in `_transfer_project_ownership`.

### 1.12 Premature Database Write and Off-by-One Dataset Count in `add_keyword`

- **Locations**: `app/api/routes/datasets.py:1526-1560`
- **Problem**: `add_keyword` (`POST /datasets/{dsid}/keywords`, `app/api/routes/datasets.py:1526-1560`) inserts and commits the `Keyword` row via `sql_find_or_add_keyword` (line 1532) before checking whether the dataset exists or calling `require_permission(..., Permission.CREATE)` (line 1538), allowing unauthorized callers to write arbitrary rows to the `keyword` table. It also queries `num_datasets` (line 1545) before `ds.keywords.append(kw_entry)` (line 1552), so `num_datasets` in the response is always off by 1.
- **Recommendation**: Verify dataset existence, `deletion_filter_for(Dataset)`, and `require_permission` before calling `sql_find_or_add_keyword`, and compute `num_datasets` after flushing `ds.keywords.append(kw_entry)`.

### 1.13 `generate_kw_query` Broken Return Type, Duplicate Rows, and Broken Count/Cursor Pagination

- **Locations**: `app/utils/sql_utils.py:295-312`, `app/api/routes/datasets.py:688-690, 714-716`, `app/api/routes/samples.py:713-724`
- **Problem**:
  - When `keyword_filter` matches no rows in `Keyword`, `generate_kw_query` returns a Python list `[]` (`app/utils/sql_utils.py:303`) instead of a SQLAlchemy `Select` statement. Callers in `GET /datasets` (`app/api/routes/datasets.py:689-690, 716`) and `GET /samples/{sample_id}/datasets` (`app/api/routes/samples.py:714-720`) immediately pass the return value to `session.exec(count_query).one()`, `.where(...)`, or `execute_resource_page(...)`, raising `ArgumentError` or `AttributeError: 'list' object has no attribute 'order_by'` (HTTP 500).
  - When multiple keywords match `Keyword.keyword.ilike(f"%{keyword_filter}%")`, `generate_kw_query` joins `DatasetKeywordLink` directly (`app/utils/sql_utils.py:306-310`) without `EXISTS` or `DISTINCT`, inflating `total` and emitting duplicate `Dataset` rows that corrupt keyset pagination cursors.
- **Recommendation**: Replace `generate_kw_query` with a single correlated `EXISTS` predicate (`select(1).select_from(DatasetKeywordLink).join(Keyword, ...).where(DatasetKeywordLink.dataset_id == Dataset.id, Keyword.keyword.ilike(...)).exists()`).

### 1.14 Datetime Keyset Cursor Deserialization and Timezone Normalization Bugs

- **Locations**: `app/services/resource_pagination.py:28, 100-101`, `app/api/routes/projects.py:151-163`, `app/api/routes/instruments.py:45-57`, `app/api/routes/users.py:213, 287-290`, `app/models/query_params.py:656, 788, 860-867`
- **Problem**:
  - `ResourcePaginationSpec.datetime_sorts` defaults to `frozenset({"created", "updated"})` (`app/services/resource_pagination.py:28`). Both `PROJECT_PAGINATION` (`app/api/routes/projects.py:159`) and `INSTRUMENT_PAGINATION` (`app/api/routes/instruments.py:53`) add `"activity": Project.last_activity_at` / `Instrument.last_activity_at` (`DateTime(timezone=True)`) without adding `"activity"` to `datetime_sorts`. When decoding a continuation cursor for `sort=activity` (`app/services/resource_pagination.py:100-101`), `as_datetime` is `False`, leaving `primary_value` as a raw `str` when comparing against a `TIMESTAMPTZ` column.
  - In `list_users` (`app/api/routes/users.py:213`), `cursor_primary_value(decoded_cursor)` is called without `as_datetime=(sort is UserSort.LAST_ACTIVE)`, leaving `decoded_primary` as a `str` when comparing against `User.last_active_at` (lines 287-290).
  - `ProjectListQuery.active_since` (`app/models/query_params.py:656`), `InstrumentListQuery.active_since` (`app/models/query_params.py:788`), and `UserListQuery.active_since` / `inactive_since` (`app/models/query_params.py:860-867`) are typed as plain `Optional[datetime]` instead of `Optional[AwareDatetime]` and are not normalized to UTC in `normalized_result_filters` / `cursor_filters`.
- **Recommendation**: Add `"activity"` to `ResourcePaginationSpec.datetime_sorts`, pass `as_datetime=(sort is UserSort.LAST_ACTIVE)` in `list_users`, and use `AwareDatetime` with UTC normalization across all timestamp query parameters.

### 1.15 Owner Facet `sort=label` Trailing-Space Mismatch Causing Duplicate Boundary Rows / Infinite Cursor Loops

- **Locations**: `app/services/resource_facets.py:64-106, 174-188`
- **Problem**: `_nonblank_sql(column)` (`app/services/resource_facets.py:64-65`) is `func.nullif(func.trim(column), "")`. In SQL, `NULLIF(a, b)` returns its first argument `a` when `a != b`. In `_facet_label_expression` (`app/services/resource_facets.py:69-71`), `full_name = _nonblank_sql(first_name + literal(" ") + last_name)` evaluates `first_name + ' ' + last_name` to `"<first_name> "` (with a trailing space) whenever `last_name` is empty or `NULL` (including all service accounts created in `app/api/routes/service_accounts.py`), and `NULLIF(TRIM(x), '')` returns the untrimmed concatenation `"<first_name> "`. Conversely, when generating the cursor in Python, `_facet_label_for_value` (`app/services/resource_facets.py:97-106`) computes `" ".join(part.strip() for part in (user.first_name, user.last_name) if part and part.strip())`, yielding `"<first_name>"` without a trailing space. On the next page for `sort=label&direction=asc`, `label_key > cursor_label_key` (`'<first_name> ' > '<first_name>'`) in `_apply_facet_cursor` (`app/services/resource_facets.py:183`) evaluates to `TRUE` for the cursor's own row, re-emitting the boundary bucket and causing an infinite pagination loop when `limit=1`.
- **Recommendation**: Change `_nonblank_sql(column)` to `func.nullif(func.trim(column), "")` wrapped around `func.trim(column)` -- i.e., `trimmed = func.trim(column); return func.nullif(trimmed, "")` -- so SQL always returns the trimmed string.

### 1.16 Case-Sensitive Username Lookup in `POST /users/resolve` and `GET /users/by-username/{username}`

- **Locations**: `app/api/routes/users.py:364-368, 457`
- **Problem**: `resolve_users` (`app/api/routes/users.py:365-366`) lowercases the input list (`normalized_usernames = [u.lower() for u in usernames]`) and queries `User.username.in_(normalized_usernames)` without `func.lower(User.username)`. Likewise, `get_user_by_username` (`app/api/routes/users.py:457`) queries `User.username == username.lower()` without `func.lower(User.username)`, failing to match mixed-case usernames in PostgreSQL.
- **Recommendation**: Use `func.lower(User.username).in_(normalized_usernames)` and `func.lower(User.username) == username.lower()` consistently with `resolve_user_by_identifier` (`app/utils/user_utils.py:67`).

### 1.17 Additional Route and Utility Bugs

- **Missing `group_name` in Account Join Requests (`app/api/routes/account.py:128-148`)**: `get_account_join_requests` returns raw `AccessGroupJoinRequest` ORM rows for `PaginatedResponse[JoinRequestRead]` without joining `AccessGroup`, leaving `group_name` always `None`.
- **`AttributeError` When Approving Non-Project Join Requests (`app/api/routes/access_groups.py:334-340`)**: `review_join_request` calls `sql_add_user_to_project(..., project_id=group.project.project_id if group.project else None, ...)`, which crashes with `AttributeError: 'NoneType' object has no attribute 'lower'` when approving a join request for a non-project access group.
- **`UnboundLocalError`, Plaintext Credential File, and Shell Injection in `app/general_utils.py`**: `get_secret` (`app/general_utils.py:60`) references `gcs_secret_path` in its error message even when `gcs_secret_name is None`, raising `UnboundLocalError`. `get_gcs_secret` (`app/general_utils.py:70-81`) checks `'root' in home` (raising `TypeError` if `HOME` is unset) and writes plaintext service account keys to `"temp_creds.json"`. `run_rclone_command` (`app/general_utils.py:19-22`) uses `subprocess.Popen(..., shell=True)` with unescaped string interpolation.
- **API Key Leak in Proposal DB Error Logs (`app/utils/user_utils.py:250-272`)**: `find_mf_user_proposals` passes `key=api_key` in the URL query string and logs `str(exc)` on failure, leaking `PROPDB_APIKEY` into logs.
- **ORM Desynchronization and Missing Previous Parent Activity Update (`app/services/activity.py:57-93`)**: `_touched_records` does not inspect `inspect(obj).attrs.project_mfid.history.deleted` / `instrument_mfid.history.deleted` when a dataset or sample is reassigned, leaving the previous parent's `last_activity_at` stale. `_record_record_activity` executes Core `connection.execute(update(...))` inside `before_flush` without updating already-loaded `Project` or `Instrument` instances in the session identity map.

---

## 2. Query, Performance, and Architectural Optimizations

### 2.1 Unconditional `deletion_blockers` Execution and Full-Group ACL Materialization on Every Project and Instrument Detail Read

- **Locations**: `app/services/resource_details.py:293, 341`, `app/api/routes/projects.py:78, 147, 257, 401`, `app/api/routes/instruments.py:89, 225, 379, 419`, `app/utils/deletion_utils.py:25-122, 164-173`
- **Problem**: Every `GET /projects/{ref}`, `GET /instruments/{ref}`, and `GET /resources/{mfid}` (for projects and instruments) calls `deletion_blockers(session, ..., resource_id)` to compute the boolean `capabilities.can_delete`, even when the caller is not the resource owner and not in admin mode. Worse, `deletion_blockers` (`app/utils/deletion_utils.py:119-122`) calls `deletion_summary(session, resource_type, resource_id)["blocking"]`. `_project_deletion_summary` and `_instrument_deletion_summary` re-fetch the entity and `AccessGroup`, count memberships and join requests, and call `_deleted_group_grants` (`app/utils/deletion_utils.py:164-173`) -- executing `SELECT resourceacl.mfid, resource.resource_type, resourceacl.permission FROM resourceacl JOIN resource ... WHERE resourceacl.accessgroup_id = :id` and materializing every dataset and sample ACL row in the project or instrument into Python memory, only for `deletion_blockers` to discard the entire `"cascaded"` dictionary.
- **Recommendation**: Short-circuit `can_delete` when the caller lacks delete authority, and implement `deletion_blockers` directly using lightweight `SELECT EXISTS(...)` queries on `Dataset` and `Sample` without calling `deletion_summary` or `_deleted_group_grants`.

### 2.2 N+1 Queries in `load_member_grants`, `list_projects`, `list_instruments`, and `assign_projects_to_user`

- **Locations**: `app/services/resource_access.py:25-83`, `app/api/routes/projects.py:203-220, 249, 396-399`, `app/api/routes/instruments.py:127-130, 226`, `app/utils/user_utils.py:213-244, 287-323`
- **Problem**:
  - `load_member_grants` (`app/services/resource_access.py:69-83`) iterates over `(ResourceACL, AccessGroup)` rows and calls `access_grant_read` (`app/services/resource_access.py:25-61`) per row, issuing a separate `SELECT` against `User`, `Project`, or `Instrument` for every ACL entry.
  - `list_projects` (`app/api/routes/projects.py:249, 396-399`) calls `_get_project_users(proj.project_id, session)` inside the per-project loop when `include_members=True` (`2N + 1` queries per page).
  - `list_instruments` (`app/api/routes/instruments.py:226`) calls `_instrument_members` inside the per-instrument loop when `include_members=True`, re-querying `fetch_acl_rows` via `may_view_members` plus `N+1` queries via `load_member_grants`.
  - `assign_projects_to_user` (`app/utils/user_utils.py:298-311`) loops over `project_ids`, querying `Project`, `User`, `AccessGroup`, and `UserAccessGroupLink` individually on each iteration.
- **Recommendation**: Batch-resolve `User`, `Project`, and `Instrument` principals by `group_type` in `load_member_grants` (at most 3 queries total), batch-load project and instrument members across the page when `include_members=True`, and batch queries in `assign_projects_to_user`.

### 2.3 Python-Side Pagination and Unnecessary Query Work in Routes and Facets

- **Locations**: `app/api/routes/projects.py:408-425`, `app/api/routes/samples.py:490-512, 541-542`, `app/services/resource_facets.py:174-199`, `app/services/resource_details.py:279, 297`, `app/api/routes/instruments.py:298-304`, `app/api/routes/users.py:467-478`
- **Python-Side Pagination in `GET /projects/{project_ref}/users` (`app/api/routes/projects.py:423-424`)**: Loads all project members into a Python list via `_get_project_users` and slices `all_users[offset:offset + limit]` in memory instead of paginating in SQL.
- **Unconditional `DatasetRead` Construction in `GET /samples` (`app/api/routes/samples.py:490-512`)**: Whenever `include_links=True` (the default), `list_samples` fetches full `Dataset` entities, resolves instrument and project references, and builds `DatasetRead` models even when `include_datasets=False` (the default, checked only at line 541).
- **Facet Keyset Predicates in `HAVING` Instead of `WHERE` (`app/services/resource_facets.py:174-199`)**: `_apply_facet_cursor` attaches keyset continuation predicates for `FacetSort.VALUE` and `FacetSort.LABEL` via `.having(...)` instead of `.where(...)`, preventing PostgreSQL from filtering rows before `GROUP BY` aggregation.
- **Duplicate Queries and Sequential Sessions Within a Single Request**: `load_instrument_detail` (`app/services/resource_details.py:279, 297`) calls `fetch_acl_rows` twice because `may_view_members` does not accept pre-fetched `acl_rows`; `read_instrument` (`app/api/routes/instruments.py:298-304`) does not pass the already-loaded `instrument` into `load_instrument_detail`; `_canonical_user_id` (`app/api/routes/users.py:467-478`) opens a separate `Session(engine)` before route handlers open a second session and re-query the same `User`.

### 2.4 Missing Indexes and Non-Sargable Predicates on Hot Filter, Join, and Facet Columns

- **Locations**: `app/models/core_models.py:68-77, 250-251, 283-284, 317-318, 517`, `app/api/routes/instruments.py:173-180`, `app/utils/user_utils.py:67, 121`
- **`Dataset.owner_orcid` and `Sample.owner_orcid`**: `owner_user_id` is indexed, but `owner_orcid` has no index on `dataset` or `sample`, even though all owner filters (`owner_id`, `owner_id_is_null`, `affiliation=owner` in `app/services/resource_filters.py:42`) and `owner` facets (`GROUP BY owner_orcid` in `app/services/resource_facets.py:26, 48`) filter or group on `owner_orcid`.
- **`UserAccessGroupLink.accessgroup_id`**: The primary key of `useraccessgrouplink` is `(user_id, accessgroup_id)` with `user_id` leading, and there is no standalone index on `accessgroup_id` for reverse lookups (`load_project_members`, `list_access_group_users`, `list_users` group filters, deletion summaries).
- **Functional Indexes on `lower("user".username)` and `lower("user".email)`**: `resolve_user_by_identifier` (`app/utils/user_utils.py:67-75`) and `list_users` (`app/api/routes/users.py:221-223`) filter on `func.lower(User.username)` and `func.lower(User.email)`, which cannot use plain B-tree indexes on `(username)` and `(email)`.
- **Non-Sargable `func.lower(...)` Filters on `Instrument` (`app/api/routes/instruments.py:173-180`)**: `Instrument.owner` and `Instrument.status` are unindexed, and `list_instruments` wraps filter columns (including the default `status="active"`) in `func.lower(...)`.

### 2.5 Middleware Double JSON Parse and Pure-Python Character Scan

- **Locations**: `app/api/main.py:96-115, 151-202`
- **Problem**: `reject_unstorable_json` (`app/api/main.py:151-202`) parses every JSON request body with `json.loads` on the main event loop thread (before FastAPI parses it again) and walks every string character-by-character in Python (`for character in text: code = ord(character)` in `_unsupported_reason`, lines 96-115). On large `scientific_metadata` payloads, this blocks the event loop and risks `RecursionError` (HTTP 500) on deeply nested JSON structures.
- **Recommendation**: Replace the character-by-character Python loop in `_unsupported_reason` with a compiled regex (`re.compile(r"[\x00\ud800-\udfff\ufdd0-\ufdef\ufffe\uffff]")`).

### 2.6 Activity Tracking Cache TTL Mismatch Causing Repeated No-Op Transactions

- **Locations**: `app/services/activity.py:16-50`
- **Problem**: `_recently_recorded` uses a 10-minute TTL (`API_KEY_USE_INTERVAL`), whereas `User.last_active_at` only updates in SQL when older than `USER_ACTIVITY_INTERVAL` (1 hour). After the first 10 minutes of activity, `_recently_recorded` expires every 10 minutes, opens a background DB session, runs a `WHERE last_active_at < cutoff` `UPDATE` that matches 0 rows, commits, and caches for another 10 minutes -- causing 5 wasted background transactions per active user per hour on every worker.
- **Recommendation**: Track user activity (`ttl=3600`) and API key activity (`ttl=600`) in separate `TTLCache` instances.

### 2.7 Uncached Hub Access Token Principal Resolution

- **Locations**: `app/utils/auth_utils.py:238-259`, `app/utils/hub_tokens.py:72-120`, `app/utils/permissions.py:121-157`
- **Problem**: While API keys and ORCID tokens cache `(user_id, principal)` in `_auth_cache`, Hub bearer tokens (`crucible_at_...`) execute synchronous database queries in `verify_hub_access_token` and `load_principal` on every request.
- **Recommendation**: Cache verified Hub access token principals keyed by `(family_id, generation)` with a short TTL and invalidate on logout/rotation.

### 2.8 Blocking the Asyncio Event Loop and Worker Threads

- **Locations**: `app/api/routes/datasets.py:214`, `app/api/routes/print.py:79-145`
- **Problem**: `async def create_dataset` (`app/api/routes/datasets.py:214`) executes synchronous SQLAlchemy queries and GCS writes on the main asyncio event loop thread (`BackgroundTasks` works identically in sync `def` route handlers). In `app/api/routes/print.py:79-145`, `print_sample_barcode` fetches 3 secrets from GCP Secret Manager on every request instead of caching them and blocks a worker thread for up to 21 seconds waiting on MQTT.
- **Recommendation**: Change `async def create_dataset` to `def create_dataset`, cache MQTT credentials, and reduce MQTT timeouts or offload publishing.

---

## 3. Schema Drift, Infrastructure, Packaging, and Code Hygiene

### 3.1 Schema Drift Between `app/models/core_models.py` and Alembic Migrations

- **Locations**: `app/models/core_models.py`, `migrations/versions/`, `migrations/data/`, `app/utils/deletion_utils.py:236-253`
- **PostgreSQL `ENUM` Columns Mapped as Plain `str`**: `DatasetLink.relationship_type`, `SampleLink.relationship_type`, `ResourceACL.permission`, `UserAccessGroupLink.role`, `AccessGroup.group_type`, and `User.platform_role` are declared as `str` / `Optional[str]` in `app/models/core_models.py` rather than matching the PostgreSQL `ENUM` types defined in `migrations/versions/`.
- **`UploadSession.expires_at` Type and Index Drift**: `UploadSession.expires_at` (`app/models/core_models.py:557`) is typed as `str` and omits `upload_sessions_expires_at_idx`, whereas `migrations/data/20260507_create_upload_sessions.sql` defines it as `TIMESTAMPTZ NOT NULL` with an index.
- **`UserPreferences` Missing Constraints and Defaults**: `UserPreferences` (`app/models/core_models.py:266-271`) omits the `server_default`s and `CheckConstraint`s (`ck_userpreferences_object`, `ck_userpreferences_size`, `ck_userpreferences_revision`) defined in `migrations/versions/20261014_5582366c0eb4_user_preferences.py`.
- **Missing `SampleKeywordLink` Model and Cleanup**: `samplekeywordlink` exists in the baseline PostgreSQL schema (`migrations/versions/20260801_037156f3a2ac_baseline_schema.py`), but has no model in `app/models/core_models.py` and is not deleted in `_hard_delete_sample` (`app/utils/deletion_utils.py:236-253`) before deleting the `Sample` row.

### 3.2 Kubernetes Configuration Defects and Resource Waste (`k8s-conf/`)

- **Hub Auth Enabled While Cleanup CronJobs Are Suspended**: `k8s-conf/configmap-main.yaml:9` and `k8s-conf/configmap-staging.yaml:7` set `HUB_AUTH_ENABLED: "true"`, while `k8s-conf/hub-auth-cleanup-main.yaml:9` and `k8s-conf/hub-auth-cleanup-staging.yaml:9` have `suspend: true`, contradicting `docs/hub_auth_security_review.md`.
- **Unused `cloud-sql-proxy` Sidecars**: `k8s-conf/deployment-k8s-conf-main.yaml:198-216` and `k8s-conf/deployment-k8s-conf-staging.yaml:206-221` each run a `cloud-sql-proxy` sidecar reserving 1 vCPU and 1-2 GiB RAM on `127.0.0.1:5432`, while `configmap-main.yaml:37` and `configmap-staging.yaml:32` set `POSTGRES_HOST: "172.31.16.10"` and bypass the sidecar.
- **Mismatched Selectors in NetworkPolicy and HPA**: `k8s-conf/api-network-policy-staging.yaml:8` selects `app: crucible-api` instead of `app: crucible-api-staging`, and `k8s-conf/horizontal-scaler.yaml:12` targets `Deployment/crucible-api` instead of `crucible-api-main` / `crucible-api-staging`.
- **Staging Storage Mount and Annotation**: `k8s-conf/deployment-k8s-conf-staging.yaml:24, 195-204` sets `gke-gcsfuse/memory-limit: 1000Gi` (vs `500Mi` in main) and mounts the production PVC `mf-storage-prod`.

### 3.3 Dockerfile and Dependency Hygiene

- **Locations**: `Dockerfile`, `pyproject.toml`, `requirements.txt`
- **Dockerfile**: Runs as `USER root` (`Dockerfile:11`), executes `curl https://rclone.org/install.sh | bash` without checksum verification (`Dockerfile:20`), copies from unpinned `ghcr.io/astral-sh/uv:latest` (`Dockerfile:23`), and copies `app` before `pyproject.toml`/`uv.lock` (`Dockerfile:28-31`), invalidating the dependency layer cache on every code edit.
- **Dependencies**: `pyproject.toml` includes `python2-secrets>=1.0.5`, `dotenv>=0.9.9` (wrapper around `python-dotenv`), and `ipykernel>=7.1.0` in production runtime dependencies, and pins `nano-crucible` to mutable `rev = "dev"` (`pyproject.toml:53`). `requirements.txt` is stale and out of sync with `pyproject.toml` and `uv.lock`.

### 3.4 Dead/Hazardous Database Bootstrap Code, Duplicated Helpers, and OpenAPI Script Mismatch

- **`app/models/build_db.py:1-16`**: Imports `DeletionRequest` from `app.models.deletion` (raising `ImportError` because `DeletionRequest` lives in `app.models.core_models`) and executes `setup_engine` and `SQLModel.metadata.create_all(engine)` at module import time against hardcoded local paths.
- **`app/utils/local_sql_utils.py` and Unused `app/utils/sql_utils.py` Helpers**: `app/utils/local_sql_utils.py` is dead duplicated code without URL-escaped passwords or pool configuration. In `app/utils/sql_utils.py`, `get_any`, `delete_link`, `add_link`, `add_or_Ignore`, `sql_add_Sample`, `sql_add_Dataset`, `build_And_Query`, `build_Or_Query`, and `get_Select_Query` have zero callers, and `sql_add_Sample` / `sql_add_Dataset` reference removed columns.
- **`scripts/export_openapi.py:18`**: Writes the SHA-256 checkpoint to `openapi/crucible-api.sha256`, whereas the repository tracks `openapi/crucible-api.json.sha256` (as verified by `tests/test_openapi_checkpoint.py:18`).

---

## Disposition (2026-10-19)

Checked against the code, the live cluster, and production data; fixed on branch `fix/api-improvements` unless noted.

### Fixed

- 1.2 Project reassignment keeps other project shares and reads the previous project from the record.
- 1.4 New uploads touch the dataset; deduplication requires the same name and content; the storage-path backfill needs a whole-name match and refuses to guess.
- 1.5 Dataset processing routes replaced by postprocessing pipelines (`GET /pipelines`, `POST /datasets/{dataset_mfid}/postprocess`, tracked `/postprocessing_runs` with `Idempotency-Key`); the old routes are deprecated wrappers with contributor checks.
- 1.6 Nine unused legacy graph routes removed; `/entity_graph_cte` walks only readable, active records, level by level, and returns `relationship_type`; lineage links reject self-links and cycles (one self-link and one two-way cycle removed from production).
- 1.7 `/extract_metadata` removed (unused SSRF surface).
- 1.8 Record files go to `RECORD_BUCKET` (`mf-record-staging` created for staging).
- 1.9 Lookups exclude pending deletions through the shared `select_active` helper.
- 1.10 `/account/verify` reports the presented key and real validity; legacy ORCID routes redirect instead of returning 500, return only to local paths, and time out ORCID calls.
- 1.14 Time filters use `UtcDatetime` (no offset means UTC); typed timestamps are returned with `Z`.
- 1.17 Join requests reworked (`docs/join_requests_design.md`), fixing the `/account/join_requests` 500; proposal-service key kept out of logs; `get_secret` raises a clear error; `build_db.py` removed.
- 2.1 Deletability checked with existence queries and only for callers who can delete.
- 2.2 Access grant principals resolved in at most three queries.
- 2.4 `dataset.owner_orcid` and `sample.owner_orcid` indexed (migration `5be2f0c8d4a1`); `useraccessgrouplink.accessgroup_id` not indexed (about 2 ms).
- 2.5 JSON check uses C-level scans and rejects deep nesting with 422.
- 2.8 `create_dataset` runs in the thread pool; a test keeps handlers synchronous.
- 3.1 `UploadSession.expires_at` typed as a timestamp.
- 3.2 Stale HPA and network policy files replaced by the live HPA (`k8s-conf/hpa-main.yaml`).
- 3.3 rclone removed, uv pinned, dependency layer cached, unused dependencies dropped.

### False or already handled

1.1, 1.3 (hash leak), 1.12, 1.15, 1.16, 3.4 (OpenAPI checksum), the 3.1 `SampleKeywordLink` claim, and the live-cluster parts of 3.2 (cleanup CronJobs run; the HPA targets `crucible-api-main`). The 1.14 cursor decoding claim was also false.

### Declined

- 1.11 Service accounts as owners: left to role rules rather than a special case.

### Deferred

- 1.3 Legacy upload token scope and legacy duplicate-name handling (contributor-only, still used by older clients).
- 1.13 `generate_kw_query` and keyword filters, pending the decision on keywords.
- 2.3 `/project_graph` encoding for very large projects (`docs/project_graph_performance_followup.md`).
- 2.6 Activity cache TTL (cosmetic).
- 2.7 Hub token principal cache.
- 3.1 Enum-typed model columns and `UserPreferences` constraints in the models (no runtime effect).
- 3.2 Unused `cloud-sql-proxy` sidecar (`docs/cloud_sql_proxy_sidecar_followup.md`); live `allow-web` policy selects no pods.
- 3.3 Running the container as non-root.
- Per-worker Prometheus counters (`docs/prometheus_multiprocess_followup.md`) and the public `/metrics` endpoint (`docs/public_metrics_endpoint_followup.md`).
- Text-typed legacy timestamps (deletion requests, audit log, ingestion requests) still use `+00:00`.
