# Self-hosting Phase 1: a fresh install works. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose up` on one workstation gives a working Crucible API backed by its own Postgres, with no Google Cloud or Foundry dependency, while the Foundry deployment keeps running unchanged.

**Architecture:** First pin every value the code silently assumes into the Foundry's Kubernetes config, then remove those defaults from the code behind explicit settings and feature flags that fail loudly when required values are missing. A schema snapshot of staging becomes the starting point for empty databases, applied by a `db upgrade` command that also runs normal Alembic upgrades. A compose bundle (Postgres, migrate job, API, Caddy, later the Hub), an admin bootstrap command, an install guide, and a CI smoke test complete the phase. Caddy is the tested default; the guide documents what any reverse proxy must do and gives an nginx example, so institutions can use their own.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy, Alembic, Postgres 17 with `vector` and `pg_trgm`, Docker Compose v2, Caddy 2, GitHub Actions, uv.

**Spec:** `docs/self_hosting_design.md` (Phase 1, "Not breaking the Foundry deployment", "Core install"). The plan was reviewed on 2026-10-22; see "Plan review" at the end for the findings and where each is handled.

**Way of working:** one task at a time. Each task is implemented, tested, reviewed by targeted Gemini reviewers through `agy`, and approved by the user before its commit and PR. Staging deploys and database operations wait for the user's go-ahead.

## Global Constraints

- The Foundry deployment must not change behavior.
- **Release order.** A release PR from `staging` to `main` carries everything on `staging`. Therefore no code task (Tasks 2 to 6) may merge into `staging` until Task 1 has been released to `main` and verified in production. Tasks 2 to 6 can be developed and reviewed on branches in the meantime.
- Every PR targets `staging`, passes the full test suite, and is verified on staging (and with `crucible-tests`) before the release PR to `main`.
- Postgres major version 17 (Cloud SQL `crucible-db` runs `POSTGRES_17`), image `pgvector/pgvector:pg17`.
- Empty environment values count as unset everywhere (`""` and whitespace-only).
- No module may read a setting, secret, bucket, or URL at import time. Values are read when used, so the app, tests, and `scripts/export_openapi.py` import without any deployment settings.
- No new runtime dependency on Google services for a self-hosted install. Google libraries stay installed in Phase 1 but are only called when a Google setting is configured.
- Disabled features keep their routes in OpenAPI and answer `503` with `{"detail": {"code": "feature_disabled", "feature": "<name>", "message": ...}}`, so the contract snapshot does not depend on deployment settings.
- Reading or dumping the staging database, and any write to staging or production, needs explicit approval from the user for that operation. Staging is reached through the local proxy on port 5433 (`DB_CONF_FILE=.env`).
- Changes to crucible-hub or nano-crucible go through their Claude sessions (`crucible-hub`, `nano-crucible`), never direct edits.
- Repository style: no em dashes, no code comments unless the reason is non-obvious, CHANGELOG entries under `Unreleased` for observable changes, regenerate `openapi/` with `PYTHONPATH=. .venv/bin/python scripts/export_openapi.py` when the contract changes, markdown prose not hard-wrapped.

## Review Focus

1. A Foundry pod where a pinned key is present but empty (`RECORD_BUCKET: ""` style) must behave as "unset", not as an empty bucket or host. Pinned in Task 2 tests.
2. A feature flag with a typo (`PRINTING_ENABLED=ture`) must stop startup with an error naming the variable, not silently mean `false`. Pinned in Task 2 tests.
3. A fresh-install run that fails midway (baseline applied, version not written) must leave the database empty so a rerun works. Pinned in Task 5 tests.
4. Running the admin bootstrap twice, or after an administrator already exists, must refuse instead of creating a second admin. Pinned in Task 6 tests.
5. With ingestion disabled, `POST /files/{id}/ingest` with `status=requested` must not leave an `IngestionRequest` row behind; with ingestion enabled but the broker unreachable, the request must end `failed` and the route answer `503`. Pinned in Task 3 tests.

---

### Task 1: Pin the Foundry configuration (Kubernetes only, no code change)

**Files:**
- Modify: `k8s-conf/configmap-main.yaml`, `k8s-conf/configmap-staging.yaml`
- Modify: `k8s-conf/deployment-k8s-conf-main.yaml`, `k8s-conf/deployment-k8s-conf-staging.yaml` (one `env` entry per new key, `configMapKeyRef` style like the existing entries)
- Modify: `k8s-conf/hub-auth-cleanup-main.yaml`, `k8s-conf/hub-auth-cleanup-staging.yaml` (`PGDB_PW_VERSION` only: `scripts/cleanup_hub_auth.py` imports `app.utils.sql_utils` and `app.utils.hub_auth_maintenance`, and only resolves `PGDB_PW`)

**Interfaces:**
- Produces: these ConfigMap keys and container env vars, read by Tasks 2 to 4. Values are what the code defaults to today, so behavior is unchanged. `RMQ_ROUTING_SUFFIX`, `UPLOAD_BUCKET`, `RECORD_BUCKET`, `ORCID_CLIENT_SECRET_VERSION`, and the database keys are already pinned and wired; leave them as they are.

| Key | main | staging | Wired into |
|---|---|---|---|
| `PGDB_PW_VERSION` | `projects/776258882599/secrets/pgcloud_pw/versions/2` | same | Deployment, CronJob |
| `CRUCIBLE_SECRET_KEY_VERSION` | `projects/776258882599/secrets/crucible_secret_key/versions/1` | same | Deployment |
| `RABBITMQ_DEFAULT_PW_VERSION` | `projects/776258882599/secrets/rabbitmq_default_pw/versions/1` | same | Deployment |
| `GCS_SA_VERSION` | `projects/776258882599/secrets/service-account-cred/versions/latest` | same | Deployment |
| `MQTT_PASSWORD_VERSION` | `projects/776258882599/secrets/mqtt-crucible-printers-passwd/versions/1` | same | Deployment |
| `PROPDB_APIKEY_VERSION` | `projects/776258882599/secrets/propdb-apikey/versions/1` | same | Deployment |
| `RMQ_HOST` / `RMQ_PORT` / `RMQ_USER` | `10.128.0.31` / `5672` / `admin` | same | Deployment |
| `GCS_PROJECT` | `crucible-prod` | same | Deployment |
| `MQTT_HOST` / `MQTT_PORT` / `MQTT_USERNAME` | `mqtt.mfdata.org` / `8883` / `crucible-printers` | same | Deployment |
| `PROPOSAL_DB_URL` | `https://foundry-admin.lbl.gov/api/JSON/sciCat-GetUser.aspx` | same | Deployment |
| `PRINTING_ENABLED`, `PROPOSAL_SYNC_ENABLED`, `INGESTION_ENABLED` | `true` | `true` | Deployment |
| `PORT` | `7777` | `8889` | Deployment |

- [ ] **Step 1:** Before editing, confirm with `kubectl exec deploy/crucible-api-staging -c crucible-api-container -- printenv` (and the same on `crucible-api-main`) that none of these keys is already set with a different value. Expected: none of them present.
- [ ] **Step 2:** Add the keys and env entries. `kubectl apply --dry-run=client -f` on each edited file: no errors. A test, `tests/test_k8s_pinned_settings.py`, loads the YAML files and asserts, for both environments, that every key in the table is in the ConfigMap with exactly that value and is wired into the Deployment (and `PGDB_PW_VERSION` into the CronJob) through a `configMapKeyRef` to that ConfigMap.
- [ ] **Step 3:** Commit, PR to `staging`, user merges. Cloud Build applies the ConfigMap and Deployment and re-applies the CronJob manifest through `scripts/deploy_hub_auth_cleanup.sh` (a merge patch of the rendered manifest that keeps the live `suspend` value). Verify on staging: `printenv` in the new pod shows every key; `kubectl get cronjob crucible-hub-auth-cleanup-staging -o jsonpath='{.spec.jobTemplate.spec.template.spec.containers[0].env[*].name}'` includes `PGDB_PW_VERSION`; `kubectl get cronjob ... -o jsonpath='{.spec.suspend}'` is unchanged; `/health/ready` is `ok`; `crucible-tests` passes.
- [ ] **Step 4:** Release to `main` in a release PR containing only Task 1 (user approves and merges). Verify the same on production. Only after this may Tasks 2 to 6 merge into `staging`.

### Task 2: Settings module and explicit secrets

**Files:**
- Create: `app/config.py`
- Modify: `app/general_utils.py` (`get_secret`, `get_gcs_secret`), `app/utils/constants.py` (remove `secret_store`), `app/utils/sql_utils.py` (`load_dotenv` only for an explicit file), every `get_secret` caller (`app/api/dependencies.py:42`, `app/api/routes/pipelines.py:53`, `app/api/routes/gcs_storage.py:43`, `app/api/routes/files.py:238`, `app/utils/rmq_utils.py:17`, `app/utils/user_utils.py:280`, `app/utils/sql_utils.py:21`, `app/utils/auth_utils.py:37`, `app/api/routes/print.py:110,206`)
- Modify: `app/utils/hub_orcid.py` (also treat `ConfigurationError` from `get_orcid_client_secret()` as `HubLoginUnavailable`)
- Delete: `app/utils/local_sql_utils.py` (unused)
- Modify: `tests/conftest.py` (hermetic environment), `tests/test_get_secret.py`, `tests/test_hub_secrets.py` (single-argument `get_secret`, full `projects/...` paths, no `secret_store`)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces in `app/config.py`:
  - `class ConfigurationError(RuntimeError)` (subclass of `RuntimeError` so existing `except RuntimeError` callers keep working)
  - `optional_env(name: str) -> str | None`: stripped value, `None` when unset or blank.
  - `env_flag(name: str, default: bool = False) -> bool`: accepts `true/false/1/0/yes/no/on/off` case-insensitively; anything else raises `ConfigurationError` naming the variable.
  - `missing_required_settings() -> list[str]`: names of required variables that are unset. Always required: `DB_SERVER`, `DB_USERNAME`, `DB_DATABASE`, `PGDB_PW`, `CRUCIBLE_SECRET_KEY`. When `INGESTION_ENABLED`: `RMQ_HOST`, `RMQ_USER`, `RMQ_ROUTING_SUFFIX`, `RABBITMQ_DEFAULT_PW`. When `PRINTING_ENABLED`: `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD`. When `PROPOSAL_SYNC_ENABLED`: `PROPOSAL_DB_URL`, `PROPDB_APIKEY`. A secret counts as set when `<NAME>`, `<NAME>_FILE`, or `<NAME>_VERSION` is set.
  - `check_settings() -> None`: raises one `ConfigurationError` listing every missing name, sorted. Called from the FastAPI lifespan in Task 4, never at import.
- Produces in `app/general_utils.py`:
  - `get_secret(name: str) -> str`. Order: `<NAME>` env value if set; else the contents of the file named by `<NAME>_FILE` (Docker secrets convention; one trailing newline stripped); else `<NAME>_VERSION`, which must be a full `projects/.../secrets/.../versions/...` path, read through `get_gcs_secret` and cached in `os.environ[name]` as today; else `ConfigurationError(f"{name} is not set")`. No hard-coded Secret Manager paths remain. `load_dotenv()` is called only when `DB_CONF_FILE` names a file (no search up the directory tree).
  - `get_gcs_secret(secret_name: str) -> str`: Application Default Credentials only; the `HOME`-contains-`root` branch and the `temp_creds.json` file are removed (the design lists "the root-HOME credential assumption" for removal).

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`:
  - `test_blank_values_are_unset`: `RECORD_BUCKET="  "` gives `optional_env("RECORD_BUCKET") is None`.
  - `test_env_flag_parses_and_rejects_typos`: `"TRUE"`, `"1"`, `"on"` give `True`; unset gives the default; `"ture"` raises `ConfigurationError` whose message contains `PRINTING_ENABLED`.
  - `test_missing_required_lists_all_names`: with only `DB_SERVER` set, the result contains `PGDB_PW` and `CRUCIBLE_SECRET_KEY`; with `INGESTION_ENABLED=true` it also contains `RMQ_HOST` and `RMQ_ROUTING_SUFFIX`.
  - `test_version_reference_satisfies_secret`: `PGDB_PW` unset and `PGDB_PW_VERSION` set means `PGDB_PW` is not missing.
  - `test_get_secret_prefers_env_then_version_then_errors`: env value returned without calling Secret Manager (monkeypatch `get_gcs_secret` to fail if called); with only `_VERSION`, the patched `get_gcs_secret` receives the exact path; with neither, `ConfigurationError`.
  - `test_get_secret_rejects_relative_version`: `PGDB_PW_VERSION="pgcloud_pw/versions/2"` raises `ConfigurationError`.
  - `test_get_secret_reads_file`: `PGDB_PW_FILE` pointing at a file containing `s3cret\n` returns `"s3cret"`; a missing file raises `ConfigurationError` naming `PGDB_PW_FILE`.
  - `test_no_parent_dotenv_is_loaded`: with `DB_CONF_FILE` unset and a `.env` containing `CRUCIBLE_TEST_SENTINEL=1` in a parent of the working directory, `get_secret("CRUCIBLE_TEST_SENTINEL")` raises `ConfigurationError`.
- [ ] **Step 2:** Run `pytest tests/test_config.py -v`; expect failures (module missing).
- [ ] **Step 3:** Implement `app/config.py`, the new `get_secret` and `get_gcs_secret`; update every caller to `get_secret("<NAME>")`; remove the module-level `rmq_pw` in `rmq_utils.py`; delete `local_sql_utils.py`; update `test_get_secret.py` and `test_hub_secrets.py`.
- [ ] **Step 4:** At the very top of `tests/conftest.py`, before any `app` import, `os.environ.setdefault` dummy values for `DB_SERVER`, `DB_USERNAME`, `DB_DATABASE`, `PGDB_PW`, `CRUCIBLE_SECRET_KEY`. Verify the suite passes with no `.env` reachable: run it with the main checkout's `.env` hidden from the worktree (the worktree lives under the main checkout, so `DB_CONF_FILE` unset and no upward search is what makes this hermetic).
- [ ] **Step 5:** Full suite passes. Commit: `Add explicit settings and secret references`.

### Task 3: Feature flags, storage settings, and import-time reads

**Files:**
- Modify: `app/utils/constants.py` (RMQ settings removed; read at call time), `app/utils/rmq_utils.py` (`send_pika_message` resolves host, port default `5672`, user, and password itself)
- Modify: `app/api/routes/files.py` (ingest route), `app/api/routes/pipelines.py` (flag; no literal `"admin"`)
- Modify: `app/api/routes/print.py` (broker settings read per request, flag)
- Modify: `app/utils/user_utils.py` (`PROPOSAL_DB_URL`, flag), `app/api/routes/account.py` (`POST /account/sync-projects` answers `503 feature_disabled` when the flag is off; user creation keeps working without proposals)
- Modify: `app/services/storage_downloads.py` (`DEFAULT_UPLOAD_BUCKET` removed, `upload_bucket()` raises when unset, `allowed_buckets()` includes the upload bucket only when set), `app/services/multipart.py` (`retire_if_eligible` tolerates an unset upload bucket), `app/utils/multipart_config.py` (no `DEFAULT_UPLOAD_BUCKET`; `UPLOAD_BUCKET` required only when `MULTIPART_UPLOAD_ENABLED`)
- Modify: `app/api/routes/gcs_storage.py` (no module-level `_DOWNLOAD_BUCKET`; `upload_bucket()` called where needed, before credentials, and `StorageNotConfigured` re-raised ahead of the generic `except Exception` blocks; `GCS_PROJECT` without default), `app/utils/deletion_utils.py` (stop importing `_DOWNLOAD_BUCKET`)
- Modify: `app/api/main.py` (exception handlers)
- Modify: `tests/conftest.py` (defaults that keep today's behavior for the existing suite: `UPLOAD_BUCKET=mf-storage-prod`, `INGESTION_ENABLED=true`, `PRINTING_ENABLED=true`, `PROPOSAL_SYNC_ENABLED=true`, `RMQ_HOST`, `RMQ_USER`, `RMQ_ROUTING_SUFFIX`, `RABBITMQ_DEFAULT_PW`, `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD`, `PROPOSAL_DB_URL`, `PROPDB_APIKEY` with dummy values), `tests/test_storage_buckets.py` (unset bucket now raises)
- Test: `tests/test_feature_flags.py` (unsets the conftest defaults it needs to)

**Interfaces:**
- Consumes: `env_flag`, `optional_env`, `ConfigurationError` from Task 2.
- Produces:
  - `class FeatureDisabled(Exception)` with attribute `feature: str`, in `app/config.py`; `require_feature(flag: str, feature: str) -> None` raises it when `env_flag(flag)` is false. Flags: `PRINTING_ENABLED` / `"printing"`, `INGESTION_ENABLED` / `"ingestion"` (ingestion and postprocessing), `PROPOSAL_SYNC_ENABLED` / `"proposal_sync"`.
  - `class StorageNotConfigured(Exception)` in `app/services/storage_downloads.py`.
  - Exception handlers in `main.py`: `FeatureDisabled` gives `503` `{"detail": {"code": "feature_disabled", "feature": ..., "message": ...}}`; `StorageNotConfigured` gives `503` `{"detail": {"code": "storage_not_configured", "message": ...}}`.
  - Ingest route order: `require_feature("INGESTION_ENABLED", "ingestion")` runs first when `status == "requested"`, before any database write or storage lookup; `status == "not_requested"` (record without dispatch) does not need the flag. The publish is wrapped like `pipelines.py:58-70`: on failure the request is set to `failed`, committed, and the route answers `503`.
  - Document the `503` responses on the affected operations (`responses=`) and regenerate `openapi/`.

- [ ] **Step 1: Write the failing tests** in `tests/test_feature_flags.py`:
  - `test_app_imports_without_storage_settings`: in a subprocess with only Task 2's five required variables, `import app.api.main` succeeds.
  - `test_print_routes_disabled`: with `PRINTING_ENABLED` unset, `GET /print/printers` and `POST /print/barcode` return `503` with `code == "feature_disabled"` and `feature == "printing"`, and no MQTT client is created.
  - `test_ingest_disabled_creates_no_request`: with `INGESTION_ENABLED` unset and `UPLOAD_BUCKET` unset, `status=requested` returns `503` `feature_disabled` (not a storage error) and the `IngestionRequest` table is empty.
  - `test_ingest_not_requested_works_when_disabled`: with `INGESTION_ENABLED` unset, `status=not_requested` records the request and returns `200`.
  - `test_ingest_broker_failure_marks_request_failed`: with the flag on and `send_pika_message` raising `pika.exceptions.AMQPConnectionError`, the route returns `503` and the request has status `failed`.
  - `test_postprocess_disabled`: `POST /datasets/{mfid}/postprocess` returns `503` `feature_disabled` and creates no `PostprocessingRun`.
  - `test_rabbitmq_user_from_settings`: with `RMQ_USER=crucible`, both the ingest publish and the postprocess publish connect as `crucible`.
  - `test_proposal_sync_disabled`: `find_mf_user_proposals("0000-0001-0000-0001") == []` without calling `requests.get`, and `POST /account/sync-projects` answers `503 feature_disabled`.
  - `test_download_without_bucket`: with `UPLOAD_BUCKET` unset, a download-link request returns `503 storage_not_configured`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement as listed under Files and Interfaces.
- [ ] **Step 4:** Full suite passes; `grep -rn "mf-storage-prod\|10.128.0.31\|mqtt.mfdata.org\|foundry-admin.lbl.gov\|crucible-prod\|776258882599" app/` returns nothing; `openapi/` regenerated. CHANGELOG `Changed`: settings explicit, Foundry defaults removed, the two `503` codes. Commit: `Make printing, ingestion, proposal sync and storage explicit settings`.

### Task 4: API address settings, startup check, container port and path

**Files:**
- Modify: `app/api/dependencies.py` (`get_api_config`, `get_api_url`, `get_api_path`, `get_api_root_path`, `get_trgm_search_threshold`, `get_orcid_client_id`)
- Modify: `app/utils/auth_utils.py` (delete the unused module-level `api_path = get_api_path()` and `api_url = get_api_url()`)
- Modify: `app/api/routes/auth.py` (build browser paths from `get_api_root_path()` so a root-mounted API never produces `//login`)
- Modify: `app/api/main.py` (FastAPI `lifespan` that calls `check_settings()`)
- Modify: `Dockerfile` (`ENV PATH="/root/.venv/bin:$PATH"`, port and workers from the environment; keep the line `COPY scripts/cleanup_hub_auth.py /root/scripts/cleanup_hub_auth.py`, which `tests/test_hub_auth_cleanup_job.py` checks)
- Test: `tests/test_api_settings.py`

**Interfaces:**
- Produces: `API_CONF_FILE` becomes optional. Environment values win over the YAML: `API_PUBLIC_URL` (scheme and host, for example `http://localhost:8080`), `API_PATH` (for example `api/v3`), `TRGM_SEARCH_THRESHOLD` (default `0.3`), `ORCID_CLIENT_ID`. With neither env nor YAML, `get_api_path()` returns `""`, `get_api_config()` returns `{}`, `get_orcid_client_id()` raises `ConfigurationError("ORCID_CLIENT_ID is not set")` when called, and `get_api_url()` raises `ConfigurationError` naming `API_PUBLIC_URL` when called. With `API_PUBLIC_URL`, `get_api_url()` is that value without trailing slash plus `get_api_root_path()`; without it, today's `https://{hostname}` from the YAML.
- The lifespan runs `check_settings()` at server startup, so importing `app.api.main` (tests, OpenAPI export) never requires settings.
- Dockerfile: the project environment is on `PATH`; no `BRANCH_NAME` port file; `CMD` runs gunicorn binding `0.0.0.0:${PORT:-8000}`; workers from gunicorn's standard `WEB_CONCURRENCY` (4 when unset). Task 1 already set `PORT` on both Foundry deployments.

- [ ] **Step 1: Write the failing tests:** `test_public_url_env_wins_and_is_normalized` (`API_PUBLIC_URL="http://lab.local:8080/"`, `API_PATH="/api/v3/"` gives `get_api_url() == "http://lab.local:8080/api/v3"`); `test_yaml_still_works` (only `API_CONF_FILE` with a temp YAML gives today's URL and path); `test_no_config_is_fine_until_needed` (no `API_CONF_FILE`, `get_api_root_path() == ""`, `get_trgm_search_threshold() == 0.3`, `get_api_url()` and `get_orcid_client_id()` raise `ConfigurationError`); `test_root_mounted_login_redirect` (with `API_PATH` empty, the `/auth/apikey` redirect `Location` starts with a single `/`); `test_lifespan_reports_missing_settings` (a `TestClient` used as a context manager with `CRUCIBLE_SECRET_KEY` removed raises `ConfigurationError` mentioning it); `test_import_needs_no_settings` (a subprocess with an empty environment apart from `PATH` imports `app.api.main`).
- [ ] **Step 2:** Run; expect failures. **Step 3:** Implement; clear `get_api_config`'s `lru_cache` in tests.
- [ ] **Step 4:** `docker build -t crucible-api:selfhost .`; `docker run --rm crucible-api:selfhost python -c "import app.api.main"` succeeds (the venv Python is on `PATH`); with `PORT=8123` and a reachable database, gunicorn logs `Listening at: http://0.0.0.0:8123`.
- [ ] **Step 5:** Full suite passes. Commit: `Read API address and port from the environment and check settings at startup`.

### Task 5: Baseline schema and the `db upgrade` command

**Files:**
- Create: `migrations/schema/baseline_b8f2d6e41c07.sql`
- Create: `app/cli/__init__.py`, `app/cli/db.py`
- Create: `scripts/compare_schema.sh`
- Modify: `migrations/env.py` (use `config.attributes["connection"]` when given, else `get_engine()`; `PRODUCTION_DB` from `ALEMBIC_PRODUCTION_DATABASE`, default `"crucible"`, blank disables the guard)
- Modify: `app/api/routes/misc.py` (`_expected_schema_revisions()` falls back to the migration heads when `EXPECTED_DB_REVISIONS` is unset; the Foundry sets it, so nothing changes there)
- Modify: `Dockerfile` (copy `alembic.ini` and the whole `migrations/` directory)
- Modify: `tests/pg_fixture.py` (recreate `vector` and `pg_trgm` after recreating the `public` schema)
- Modify: `migrations/README.md` (fresh installs, baseline, schema comparison)
- Test: `tests/test_db_upgrade.py` (Postgres-gated with `CRUCIBLE_TEST_PG_URL`)

**Interfaces:**
- Produces in `app/cli/db.py`:
  - `BASELINE_REVISION = "b8f2d6e41c07"` (staging and production are both at this revision; `EXPECTED_DB_REVISIONS` in both ConfigMaps).
  - `upgrade_database(engine, alembic_ini: str = "alembic.ini") -> str`: returns `"initialized"` when the `public` schema had no tables, `"upgraded"` when `alembic_version` exists. Raises `RuntimeError` when tables exist without `alembic_version`. On an empty database, one transaction applies the baseline SQL, resets `search_path` to `public`, creates `public.alembic_version` and inserts `BASELINE_REVISION`, and commits. Then `alembic.command.upgrade(cfg, "head")` runs with `cfg.attributes["connection"]` set to a fresh connection from `engine` (outside any open transaction, so migrations that use `autocommit_block()` work).
  - `python -m app.cli.db upgrade` calls it with `get_engine()` and prints the result.
- Produces `scripts/compare_schema.sh URL_A URL_B`: schema-only `pg_dump` of both with the `postgres:17` client image (`--schema-only --no-owner --no-privileges`), drops comment lines, blank lines, `SET` lines, the `set_config('search_path'...)` line, and `\restrict`/`\unrestrict` lines, diffs, exits `1` on any difference.

- [ ] **Step 1 (needs user approval for a read-only staging dump):** Confirm `SELECT version_num FROM alembic_version` on staging is `b8f2d6e41c07`. `pg_dump --schema-only --no-owner --no-privileges` of `crucible-staging` through the proxy with a Postgres 17 client. Remove `\restrict`/`\unrestrict` lines. Keep the `CREATE EXTENSION` statements for `vector` and `pg_trgm`; remove any Cloud SQL-only objects. Append the seed rows, schema-qualified: `INSERT INTO public.accessgroup (group_name, group_type) VALUES ('public', 'system') ON CONFLICT (group_name) DO NOTHING;` (confirm the `group_type` value on staging), plus any other row the code looks up by fixed name (grep `app/` for fixed group or role names). Do not include an `alembic_version` row.
- [ ] **Step 2: Write the failing tests** in `tests/test_db_upgrade.py`: `test_empty_database_is_initialized_to_head` (returns `"initialized"`, `alembic_version` equals the script head, the `public` group exists, both extensions exist); `test_second_run_is_upgrade_noop` (returns `"upgraded"`); `test_tables_without_revision_refused` (one stray table, `RuntimeError`, table still the only one); `test_failure_midway_leaves_database_empty` (monkeypatch the version insert to raise, then zero tables in `public`); `test_runs_against_given_engine` (with `DB_SERVER` pointing nowhere, `upgrade_database(test_engine)` still succeeds); `test_guard_name_configurable` (with `ALEMBIC_PRODUCTION_DATABASE=""` the guard does not fire on a database named `crucible`); `test_readiness_uses_heads_when_unset` (with `EXPECTED_DB_REVISIONS` unset and an empty `alembic_version`, `/health/ready` reports `behind`).
- [ ] **Step 3:** Run with `CRUCIBLE_TEST_PG_URL` against `pgvector/pgvector:pg17`; expect failures. **Step 4:** Implement.
- [ ] **Step 5 (needs the same approval):** Initialize a local `pgvector:pg17` database with `upgrade_database`, then `scripts/compare_schema.sh <local> <staging>`. Expected: no differences. Record the command in `migrations/README.md`.
- [ ] **Step 6:** Full suite and gated tests pass. Commit: `Add a schema baseline and db upgrade command for fresh installs`.

### Task 6: Admin bootstrap

**Files:**
- Modify: `app/utils/user_utils.py` (extract user provisioning), `app/api/routes/users.py` (`POST /users` uses it)
- Create: `app/cli/admin.py`
- Test: `tests/test_admin_bootstrap.py`

**Interfaces:**
- Produces in `app/utils/user_utils.py`: `provision_user(session, *, unique_id: str, first_name: str, last_name: str, email: str | None, username: str | None, project_groups: Sequence[AccessGroup] = (), platform_role: PlatformRole | None = None) -> User`. It does exactly what `POST /users` does today inside its session (user row, identity group, owner link, contributor links to the given project groups), does not commit, and does not generate usernames. `POST /users` keeps its checks before calling it (`409` on existing `unique_id`, identity group, username or email; proposal discovery; `is_service_account=False`) and its responses unchanged; existing `POST /users` tests prove it.
- Produces: `python -m app.cli.admin create-admin --first-name F --last-name L [--email E] [--username U] [--orcid O] [--allow-additional]`.
  - Without `--orcid` the user gets an MFID (`get_mfid_value()`); with it, the value must pass `is_orcid`.
  - `--username` is validated with `validate_username_format`; when omitted, one is generated with `generate_unique_username`. `--email` is lowercased and stripped.
  - Sets `platform_role` to `PlatformRole.ADMIN`, commits with `commit_user`, and only then mints the key with `find_or_add_api_access` (it uses its own session and needs the committed user).
  - Prints one JSON object `{"unique_id": ..., "username": ..., "api_key": ...}` to stdout; messages go to stderr.
  - Exits `1` with a message when any user already has `platform_role == admin`, unless `--allow-additional`.

- [ ] **Step 1: Write the failing tests** (SQLite `db_engine` fixture, call the CLI entry function directly): `test_create_admin_creates_user_group_and_key` (admin role, identity group owned by the user, one `ApiToken`, stdout parses as JSON with those three keys); `test_second_admin_refused` (exit `1`, still one admin); `test_allow_additional`; `test_orcid_validated` (`--orcid not-an-orcid` exits non-zero, nothing created); `test_invalid_username_rejected`.
- [ ] **Step 2:** Run; expect failures. **Step 3:** Implement. **Step 4:** Full suite passes (existing `POST /users` tests unchanged). Commit: `Add an admin bootstrap command`.

### Task 7: Compose bundle and install guide

**Files:**
- Create: `deploy/compose/compose.yaml`, `deploy/compose/.env.example`, `deploy/compose/Caddyfile`, `deploy/compose/README.md` (install guide)
- Modify: `docs/README.md` (link the guide), `README.md` (one line pointing to it)

**Interfaces:**
- Consumes: settings from Tasks 2 to 4, `python -m app.cli.db upgrade` (Task 5), `python -m app.cli.admin create-admin` (Task 6). Commands run with the image's venv Python, which Task 4 put on `PATH`.
- Produces services:
  - `db`: `pgvector/pgvector:pg17`, named volume, `pg_isready` healthcheck, `POSTGRES_DB=crucible`.
  - `migrate`: built from the repository root, runs `python -m app.cli.db upgrade`, `depends_on: db: condition: service_healthy`, `ALEMBIC_PRODUCTION_DATABASE=""`.
  - `api`: same image, `PORT=8000`, `API_PUBLIC_URL=http://localhost:${CRUCIBLE_HTTP_PORT:-8080}` (interpolated so changing the port keeps them in step), `API_PATH=api/v3`, all feature flags off, `HUB_AUTH_ENABLED=false`, no `EXPECTED_DB_REVISIONS` (readiness compares against the image's migration heads, Task 5), `depends_on: migrate: condition: service_completed_successfully`, healthcheck on `http://127.0.0.1:8000/health/ready` with Python's `urllib` (the image has no curl).
  - `proxy`: `caddy:2` publishing `${CRUCIBLE_HTTP_PORT:-8080}:80`, `depends_on: api: condition: service_healthy`. Routes `/api/v3` and `/api/v3/*` to `api:8000` with the prefix stripped (`handle_path`), as `crucible-nginx` does today.
- `.env.example` lists every variable with a one-line explanation; secrets are placeholders the guide tells you to generate (`openssl rand -hex 32`).
- Install guide covers: prerequisites (Docker Engine with Compose v2), configuration, `docker compose up -d --build --wait`, creating the administrator, checking `/api/v3/health/ready`, pointing nano-crucible at `http://<host>:8080/api/v3` with the API key, what is not available yet (file storage arrives in Phase 2; Hub sign-in needs ORCID over HTTPS until Phase 3), backups with `pg_dump`, upgrading (rebuild and `up`; `migrate` runs automatically), and "Using your own reverse proxy": the routing rules (`/api/v3/` to the API with the prefix stripped, `/hub` to the Hub with the prefix kept, client address headers passed, body size and timeouts sufficient for uploads) and an nginx example derived from `crucible-nginx`.

- [ ] **Step 1:** Write the files. **Step 2:** From a clean checkout: `cp .env.example .env`, fill secrets, `docker compose up -d --build --wait`; all services healthy; `curl -s localhost:8080/api/v3/health/ready` shows `"status":"ok"` and `"schema_state":"current"`; `curl -s localhost:8080/api/v3` reaches the API. **Step 3:** `docker compose run --rm api python -m app.cli.admin create-admin --first-name Ada --last-name Admin` prints the JSON; `GET /api/v3/account/profile` with the key returns the admin. **Step 4:** Change `CRUCIBLE_HTTP_PORT` to `18080`, `up` again, repeat the health check on the new port. **Step 5:** `docker compose down -v`. Commit: `Add a docker compose bundle and install guide`.

### Task 8: Smoke test and CI

**Files:**
- Create: `scripts/selfhost_smoke.py`
- Modify: `.github/workflows/tests.yml` (added in #351: one `pytest` job with a `pgvector/pgvector:pg16` service; it does not set `CRUCIBLE_TEST_PG_URL`, so the Postgres-gated tests are skipped today)

**Interfaces:**
- Produces `python scripts/selfhost_smoke.py --api-url URL --api-key KEY` (all paths under `/api/v3`): checks health (`status == "ok"`, `schema_state == "current"`), reads `GET /account/profile`, creates a project (with `project_lead` set to the admin), a sample and a dataset in it, links them, reads the dataset back by MFID and through `GET /datasets?mfid=`, creates a public sample and reads it as another user's view would allow (proves the seeded `public` group), and confirms an unauthenticated request is refused. Exits `0` on success, non-zero with the failing step named.
- Changes to `tests.yml`, keeping its pinned action versions and permissions:
  - `pytest` job: service image `pgvector/pgvector:pg17`; create one more database with both extensions and set `CRUCIBLE_TEST_PG_URL` to it so the gated tests, including Task 5's, run.
  - New `compose-smoke` job: generates `.env` from `.env.example` with random secrets, `docker compose up -d --build --wait`, `create-admin`, runs the smoke script against the proxy, prints `docker compose logs` on failure, `down -v` always.

- [ ] **Step 1:** Write the smoke script; run it against Task 7's stack; exit `0`. With a wrong key it exits non-zero naming the step.
- [ ] **Step 2:** Update the workflow; open the PR; both jobs pass and the gated tests report as run, not skipped. Commit: `Run Postgres tests and a self-hosted smoke test in CI`.

### Task 9: Hub in the compose bundle (depends on crucible-hub)

**Files:**
- Modify: `deploy/compose/compose.yaml`, `deploy/compose/.env.example`, `deploy/compose/Caddyfile`, `deploy/compose/README.md`

**Interfaces:**
- Consumes from the crucible-hub session (request by message, do not edit the hub repository): (a) accepting a plain-HTTP internal API URL such as `http://api:8000` when explicitly configured (today `CRUCIBLE_API_BASE_URL` must be HTTPS or localhost); (b) how its session-store migrations run in a container; (c) the image to use (a release tag to build from `https://github.com/MolecularFoundryCrucible/crucible-hub.git#<tag>`, or a published image); (d) which URLs it uses for browser redirects versus server-to-server token calls.
- Produces: a `hub` service behind a compose profile (`profiles: ["hub"]`) so the default `up` and the CI smoke job do not need Hub secrets or images. With the profile: `HUB_DATA_SOURCE=hub-auth`, sessions in a `hub` database on the same Postgres, Caddy `handle /hub*` to `hub:3000` without stripping, and the API's `HUB_AUTH_*` settings (`HUB_AUTH_ENABLED`, `HUB_AUTH_CLIENT_ID`, `HUB_AUTH_CLIENT_SECRET`, `HUB_AUTH_CALLBACK_URI`, `HUB_AUTH_PUBLIC_API_URL`, `HUB_AUTH_ORCID_CLIENT_ID`, `HUB_AUTH_ORCID_CLIENT_SECRET`, `HUB_AUTH_ORCID_ISSUER`) passed to `api`. The guide states that Hub sign-in requires an HTTPS public address unless everything runs on `localhost` (`_trusted_public_url` in `app/utils/hub_login.py`), and registering the callback with ORCID.

- [ ] **Step 1:** Message `crucible-hub` with (a) to (d). Wait for answers; record them here.
- [ ] **Step 2:** Add the profile; `docker compose --profile hub up -d --build --wait`; `curl -s localhost:8080/hub/health` returns `200`. With ORCID configured (manual check by the user), sign-in reaches the Hub home page.
- [ ] **Step 3:** Commit: `Add the Hub to the compose bundle as an optional profile`.

### Task 10: Close out Phase 1

**Files:**
- Modify: `CHANGELOG.md`, `docs/self_hosting_design.md` (status line), `migrations/README.md`, `docs/README.md`

- [ ] **Step 1:** CHANGELOG `Added`: compose bundle, `db upgrade`, `create-admin`, CI. `Changed`: settings and secrets, feature flags, port, readiness without `EXPECTED_DB_REVISIONS`. Operator note: required settings for any non-Foundry deployment.
- [ ] **Step 2:** Design doc status: Phase 1 complete, with the PR numbers. Commit: `Record Phase 1 completion`.

---

## Plan review (2026-10-22)

Eight Gemini 4 Argon (High) reviewers read the plan against the code independently (reports kept outside the repository). Every recurring claim was checked against the code before it was accepted. Reviewers 6 and 8 partly reviewed settings and steps that do not exist in this plan, and their unique claims were discarded unless confirmed.

### Accepted, and where they are handled

| # | Finding | Handled in |
|---|---|---|
| 1 | A `staging` to `main` release carries everything on `staging`, so code tasks must not merge into `staging` before Task 1 is in production | Global Constraints, Task 1 Step 4 |
| 2 | The cleanup CronJob needs `PGDB_PW_VERSION`, and its live environment must be verified | Task 1 Files and Step 3 |
| 3 | `RMQ_ROUTING_SUFFIX` must be required when ingestion is on, or messages go to `ingestion-None-*` | Task 2 `missing_required_settings` |
| 4 | Import-time reads crash once defaults go: `gcs_storage.py:28` (`_DOWNLOAD_BUCKET`, also imported by `deletion_utils.py:16`) and `auth_utils.py:45-46` (unused `api_path`, `api_url`) | Global Constraints, Tasks 3 and 4 |
| 5 | `multipart_config.py` imports `DEFAULT_UPLOAD_BUCKET` | Task 3 |
| 6 | `allowed_buckets()` and `retire_if_eligible()` read the upload bucket unconditionally | Task 3 |
| 7 | The ingest route has no broker error handling; `status=not_requested` needs no broker; `pipelines.py:52` also hard-codes `user="admin"` | Task 3 Interfaces and tests |
| 8 | Existing tests break: `test_get_secret.py`, `test_hub_secrets.py`, storage, print, postprocessing, proposal logging, and `test_hub_auth_cleanup_job.py` (checks a Dockerfile `COPY` line) | Tasks 2, 3, 4 Files |
| 9 | `get_gcs_secret` still has the `HOME`-contains-`root` branch and writes `temp_creds.json` | Task 2 |
| 10 | `load_dotenv()` searches parent directories for `.env` | Task 2 |
| 11 | Checking settings at import breaks the OpenAPI export and tools | Task 2 (`check_settings` not at import), Task 4 (lifespan) |
| 12 | `get_orcid_client_id()` raises `KeyError` without a config file; root-mounted paths produce `//login` | Task 4 |
| 13 | `migrations/env.py` ignores the engine given to `upgrade_database` | Task 5 |
| 14 | `pg_dump` output sets an empty `search_path` | Task 5 |
| 15 | `pg_fixture.fresh_schema` drops the extensions with the schema | Task 5 |
| 16 | The project venv is not on the image's `PATH` | Task 4 Dockerfile |
| 17 | `/health/ready` reports `current` when `EXPECTED_DB_REVISIONS` is unset | Task 5 (`misc.py`), Task 7 |
| 18 | The admin API key is minted in a separate session, so the user must be committed first | Task 6 |
| 19 | Hub sign-in needs HTTPS off `localhost` and the API's `HUB_AUTH_*` settings; a default Hub service would break `up` without secrets | Task 9 (profile, settings, guide) |
| 20 | Caddy must also route `/api/v3` without a trailing slash; port changes must keep URLs in step | Task 7 |

Also adopted: `ConfigurationError` subclasses `RuntimeError`; `POST /account/sync-projects` answers `503` when disabled; the Dockerfile copies the whole `migrations/` directory; the smoke test uses `/account/profile` and sets `project_lead`.

### Checked and rejected

- Staging is not at `5e8a2c4f9b10`; both ConfigMaps expect `b8f2d6e41c07`.
- Cloud Build does apply the ConfigMap and Deployment (`cloudbuild.yaml`), and `deploy_hub_auth_cleanup.sh` merge-patches the full rendered CronJob manifest, so environment changes reach the CronJob while `suspend` is preserved.
- `RECORD_BUCKET` is already stripped (`gcs_record_utils.py`).
- `ORCID_CLIENT_SECRET_VERSION` is present in the main ConfigMap.
- `MIGRATIONS_DIR` in `misc.py` resolves to `/root/migrations` in the image.
- Secret Manager is not called per request: `get_secret` caches the value in `os.environ`.
- Several reviewers described a `typer` CLI, a `postgres:17` image, `RMQ_QUEUE_NAME`, `SECRET_STORE`, or `/users/me` calls; none of these are in the plan or the code.
