# Routes audit, October 2026

Status: open. Audit of `main` at 3.3.0 (commit 9361f65), to work through after the first cleanup pass. 176 routes in 22 routers, all mounted in `app/api/main.py`; 26 are deprecated. No router or dependency is orphaned. The deprecated routes log their callers' user agents, which shows who still depends on them.

## Fix first

- `GET /regenerate_apikey` (deprecated alias, `app/api/routes/auth.py:298`) rotates the caller's API key from the browser login cookie. A link or image on any page can trigger it and lock a user out of their key. Remove it, or make it refuse, before 4.0.
- `GET /keywords`, `GET /keywords/{keyword}` and `GET /ingestors` (`app/api/routes/misc.py`) require no sign-in. Decide whether that is intended.
- The deprecation message of `POST /datasets/{id}/upload/complete` points to `/upload/multipart/initiate`; its replacement is `/upload/multipart/complete` (`app/api/routes/gcs_storage.py:311`).

## Overlapping routes

- Uploads: the legacy `/upload/initiate`, `/upload/initiate/multipart`, `/upload/complete` sit next to the current `/upload/multipart/*`; `upload/initiate/multipart` and `upload/multipart/initiate` are easy to confuse. The legacy three retire in 4.0.
- Postprocessing: four per-pipeline POSTs (`carrier_segmentation`, `insitu_spec_aggregation`, `rga_analysis`, `mosaic_stitch`) duplicate `POST /datasets/{id}/postprocess`.
- Access: three generations (`/access_groups/{g}/join`, `/resources/{mfid}/access_groups`, `/users/{u}/access_groups/{g}`) next to `/resources/{mfid}/access` and project membership.
- API keys can be read or rotated in five places (`/auth/apikey`, `/account/apikey`, `/users/{u}/apikey`, their `regenerate` and `verify` variants).
- `POST /resources/{mfid}/delete` (asks for deletion) and `DELETE /resources/{mfid}` (hard deletion) do different things under the same path.
- Graph: `/entity_graph_cte/{mfid}` and `/project_graph/{ref}` overlap `/resources/{mfid}/graph`. nano-crucible, the Graph Explorer and Crucible Labs still call them ([graph routes migration](graph_routes_migration.md)).
- Users: `/users/by-username/{u}`, `/users/resolve`, `/users/{u}/datasets`, `/users/{u}/samples` (deprecated), and `/users/{u}/datasets/{mfid}` (not deprecated), each covered by a general route.
- `GET /health` is an alias of `/health/ready`.

## Untested routes

- No HTTP test: `/health`, `/keywords`, `/keywords/{k}`, `/ingestors`, dataset keywords GET and POST, thumbnail PATCH and DELETE, `DELETE /account/recent-resources/{mfid}`, `POST /access_groups`, `GET /access_groups/{g}`, `GET /access_groups/{g}/users`, the deprecated `/datasets/{id}/files/{id}/ingest`.
- Called as functions only, so authentication and serialization are not exercised: `/resources/{mfid}/links`, `/resources/metadata/search`, `/deletion_audit`, `/deletion_audit/{id}`, `POST /datasets/first_thumbnails`.
- `POST /datasets/{id}/upload/multipart/complete` runs only in the S3 integration test (needs `S3_TEST_ENDPOINTS`).

## Inconsistencies

- Path style: snake_case (`access_groups`, `join_requests`) and kebab-case (`recent-resources`, `sync-projects`, `by-username`); `download_link` on files, `download_links` on datasets; verbs in paths (`/delete`, `/withdraw`, `/regenerate`); `cte` in `/entity_graph_cte`.
- Path parameters: `{file_id}` in `files.py`, `{file_mfid}` in `multipart.py`; `{mfid}`, `{resource_mfid}` and `{entity_mfid}` for the same thing.
- Lists: about 15 routes return bare lists (for example `/datasets/{id}/files`, `/resources/{mfid}/access`, `/pipelines`, `/embeddings/search`) while others return `PaginatedResponse`. `/datasets` and `/samples` accept both cursor and offset pagination.
- Errors: most routes answer `detail` as a plain string; files, uploads, Hub auth, pipelines and ingestion answer `{code, message}`.
- Deprecation: only some deprecated routes set the `Deprecation` header; the postprocess aliases, auth aliases and `/access_groups/{g}/join` are flagged only in OpenAPI.
- Handler names mix `get_`, `read_`, `list_`; `search_resource_metadata_v2` has no v1.

## For 4.0

- Remove the 26 deprecated routes, keeping `POST /datasets/{id}/files` (catalog records for files stored elsewhere), together with `LEGACY_UPLOAD_*` and `storage.legacy_upload_token_downscope`.
- Drop deprecated parameters and fields: `owner`/`owner_orcid`/`owner_id` variants, `include_datasets` (and embedded datasets on samples by default), `resource_id`/`dataset_id`/`sample_id` response fields, email in user paths, offset pagination on `/datasets` and `/samples`, the `project_lead_email` column.
- One list envelope, one error body (`code`, `message`), one path style.
- Agree with nano-crucible first: the graph routes, the `/health` alias, `/users/{u}/datasets/{mfid}`.
