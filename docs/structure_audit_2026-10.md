# Structure audit, October 2026

Status: open. Audit of `main` at 3.3.0, to work through after the first cleanup pass, which removed unused libraries, dead code, one-off scripts, the tracked OpenAPI file, and stale build files. The layout (`app/api/routes`, `services`, `models`, `utils`, `storage`, `cli`) is sound, and almost no code is dead.

## Split large files

| File | Lines |
| --- | ---: |
| `app/api/routes/datasets.py` | 1598 |
| `app/api/routes/resources.py` | 1240 |
| `app/api/routes/deletion.py` | 1060 |
| `app/models/query_params.py` | 1040 |
| `app/api/routes/users.py` | 955 |
| `app/utils/auth_utils.py` | 819 |
| `app/services/resource_graph.py` | 795 |

Split routes by sub-resource (for example datasets: files, thumbnails, keywords, samples), and move query logic into `app/services`.

## Move or rename

- `app/api/routes/datasets.py` imports `start_postprocessing_run` from `routes/pipelines.py`. Move it to `app/services`.
- `app/utils/gcs_record_utils.py` already writes through `app.storage`; only its name says GCS. Rename it (for example `record_store.py`).
- `app/general_utils.py` belongs in `app/utils/`. `app/utils` also holds service-like code (`user_utils`, `project_utils`, `deletion_utils`) that fits `app/services`.
- `app/api/routes/gcs_storage.py` holds the legacy upload routes, the only Google-specific code outside `app/storage`. It goes with 4.0.

## Configuration

- `app/conf/*.yaml` (`API_CONF_FILE`) is a fallback for settings that environment variables already cover; `gcs_config_name` in it is read nowhere. It can go once the Foundry deployment sets `ORCID_CLIENT_ID` and `TRGM_SEARCH_THRESHOLD` directly.
- The Secret Manager `NAME_VERSION` form stays while the Foundry uses it; mounted files (`NAME` in the secrets directory) are the portable form.
- The image runs as root under `/root`. A non-root user needs the deploy repository's CronJob command and paths changed at the same time.
- The `uv` version is pinned twice: the Dockerfile and `.github/workflows/*.yml`.
- Dependabot does not watch the Compose images (`deploy/compose/compose.yaml`) or the CI service images. Add a `docker` entry for `/deploy/compose`.
- `.gitignore` is mostly a generic Python template (Django, Flask, Sphinx and others). Trim it to what this repository produces.

## Migrations

- 41 Alembic revisions with one head. Do not squash: staging and production exist, and `migrations/schema/baseline_*.sql` already covers fresh installations.
- `migrations/data/` holds 47 dated one-time scripts (May to October 2026). Move the ones that have run in production to `migrations/data/archive/` (or delete them; git keeps them). This needs a list of what ran.

## Tests

- 68 tests skip unless a test database or S3 endpoint is configured; none skip unconditionally. The database URL has four names (`CRUCIBLE_TEST_PG_URL`, `HUB_AUTH_TEST_DATABASE_URL`, `SLUG_INDEX_TEST_DATABASE_URL`, `EMAIL_INDEX_TEST_DATABASE_URL`). Use one.
- Possible overlaps to check: `test_project_rename.py` and `test_project_rename_resolves_live.py`; the five `test_multipart_*` files; `test_hub_tokens.py` and `test_hub_auth_cleanup_job.py`.
- Tests for the legacy upload routes (`test_gcs_multipart_endpoint.py`) go with them in 4.0.
