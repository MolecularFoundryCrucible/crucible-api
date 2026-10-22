# Self-hosting Phase 1: a fresh install works. Implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `docker compose up` on one workstation gives a working Crucible API backed by its own Postgres, with no Google Cloud or Foundry dependency, while the Foundry deployment keeps running unchanged.

**Architecture:** First pin every value the code silently assumes into the Foundry's Kubernetes config, then remove those defaults from the code behind explicit settings and feature flags that fail loudly when required values are missing. A schema snapshot of staging becomes the starting point for empty databases, applied by a `db upgrade` command that also runs normal Alembic upgrades. A compose bundle (Postgres, migrate job, API, Caddy, later the Hub), an admin bootstrap command, an install guide, and a CI smoke test complete the phase.

**Tech Stack:** Python 3.11, FastAPI, SQLModel/SQLAlchemy, Alembic, Postgres 17 with `vector` and `pg_trgm`, Docker Compose v2, Caddy 2, GitHub Actions, uv.

**Spec:** `docs/self_hosting_design.md` (Phase 1, "Not breaking the Foundry deployment", "Core install").

## Global Constraints

- The Foundry deployment must not change behavior. Task 1 is deployed to staging and production before any code task that removes a default is merged to `main`.
- Every PR targets `staging`, passes the full test suite, and is verified on staging (and with `crucible-tests`) before the release PR to `main`.
- Postgres major version 17 (Cloud SQL `crucible-db` runs `POSTGRES_17`), image `pgvector/pgvector:pg17`.
- Empty environment values count as unset everywhere (`""` and whitespace-only).
- No new runtime dependency on Google services for a self-hosted install. Google libraries stay installed in Phase 1 but are only called when a Google setting is configured.
- Disabled features keep their routes in OpenAPI and answer `503` with `{"detail": {"code": "feature_disabled", "feature": "<name>", "message": ...}}`, so the contract snapshot does not depend on deployment settings.
- Reading or dumping the staging database, and any write to staging or production, needs explicit approval from the user for that operation. Staging is reached through the local proxy on port 5433 (`DB_CONF_FILE=.env`).
- Changes to crucible-hub or nano-crucible go through their Claude sessions (`crucible-hub`, `nano-crucible`), never direct edits.
- Repository style: no em dashes, no code comments unless the reason is non-obvious, CHANGELOG entries under `Unreleased` for observable changes, regenerate `openapi/` with `PYTHONPATH=. .venv/bin/python scripts/export_openapi.py` when the contract changes, markdown prose not hard-wrapped.

## Review Focus

1. A Foundry pod where a pinned key is present but empty (`RECORD_BUCKET: ""` style) must behave as "unset", not as an empty bucket or host. Pinned in Task 2 tests.
2. A feature flag with a typo (`PRINTING_ENABLED=ture`) must stop startup with an error naming the variable, not silently mean `false`. Pinned in Task 2 tests.
3. A fresh-install run that fails midway (baseline applied, stamp not written) must leave the database empty so a rerun works. Pinned in Task 5 tests.
4. Running the admin bootstrap twice, or after an administrator already exists, must refuse instead of creating a second admin. Pinned in Task 6 tests.
5. With ingestion disabled, `POST` ingest must not leave an `IngestionRequest` row behind; with ingestion enabled but the broker unreachable, the request must end `failed` and the route answer `503`. Pinned in Task 3 tests.

---

### Task 1: Pin the Foundry configuration (Kubernetes only, no code change)

**Files:**
- Modify: `k8s-conf/configmap-main.yaml`, `k8s-conf/configmap-staging.yaml`
- Modify: `k8s-conf/deployment-k8s-conf-main.yaml`, `k8s-conf/deployment-k8s-conf-staging.yaml` (one `env` entry per new key, `configMapKeyRef` style like the existing entries)
- Modify: `k8s-conf/hub-auth-cleanup-main.yaml`, `k8s-conf/hub-auth-cleanup-staging.yaml` (secret version keys the cleanup script needs)

**Interfaces:**
- Produces: these ConfigMap keys and container env vars, read by Tasks 2 to 4. Values are what the code defaults to today, so behavior is unchanged.

| Key | main | staging |
|---|---|---|
| `PGDB_PW_VERSION` | `projects/776258882599/secrets/pgcloud_pw/versions/2` | same |
| `CRUCIBLE_SECRET_KEY_VERSION` | `projects/776258882599/secrets/crucible_secret_key/versions/1` | same |
| `RABBITMQ_DEFAULT_PW_VERSION` | `projects/776258882599/secrets/rabbitmq_default_pw/versions/1` | same |
| `GCS_SA_VERSION` | `projects/776258882599/secrets/service-account-cred/versions/latest` | same |
| `MQTT_PASSWORD_VERSION` | `projects/776258882599/secrets/mqtt-crucible-printers-passwd/versions/1` | same |
| `PROPDB_APIKEY_VERSION` | `projects/776258882599/secrets/propdb-apikey/versions/1` | same |
| `RMQ_HOST` / `RMQ_PORT` / `RMQ_USER` | `10.128.0.31` / `5672` / `admin` | same |
| `GCS_PROJECT` | `crucible-prod` | same |
| `MQTT_HOST` / `MQTT_PORT` / `MQTT_USERNAME` | `mqtt.mfdata.org` / `8883` / `crucible-printers` | same |
| `PROPOSAL_DB_URL` | `https://foundry-admin.lbl.gov/api/JSON/sciCat-GetUser.aspx` | same |
| `PRINTING_ENABLED`, `PROPOSAL_SYNC_ENABLED`, `INGESTION_ENABLED` | `true` | `true` |
| `PORT` | `7777` | `8889` |

- [ ] **Step 1:** Before editing, confirm with `kubectl exec deploy/crucible-api-staging -c crucible-api-container -- printenv` that none of these keys is already set with a different value, and grep `scripts/cleanup_hub_auth.py` imports to list which secrets the cleanup job resolves (at least `PGDB_PW`; add `CRUCIBLE_SECRET_KEY_VERSION` if `app.utils.auth_utils` is imported).
- [ ] **Step 2:** Add the keys and env entries. Run `kubectl apply --dry-run=client -f` on each edited file; expect no errors.
- [ ] **Step 3:** PR to `staging`. After the Cloud Build deploy, `printenv` in the staging pod shows every key; `/health/ready` is `ok`; `crucible-tests` passes.
- [ ] **Step 4:** Release to `main` in its own release PR (with user approval). Verify the same on production. Tasks 2 to 4 may merge to `main` only after this.

### Task 2: Settings module and explicit secrets

**Files:**
- Create: `app/config.py`
- Modify: `app/general_utils.py` (`get_secret`), `app/utils/constants.py` (remove `secret_store`), every `get_secret` caller (`app/api/dependencies.py:42`, `app/api/routes/pipelines.py:53`, `app/api/routes/gcs_storage.py:43`, `app/api/routes/files.py:238`, `app/utils/rmq_utils.py:17`, `app/utils/user_utils.py:280`, `app/utils/sql_utils.py:21`, `app/utils/auth_utils.py:37`, `app/api/routes/print.py:110,206`)
- Delete: `app/utils/local_sql_utils.py` (unused)
- Modify: `tests/conftest.py` (hermetic environment)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces in `app/config.py`:
  - `class ConfigurationError(RuntimeError)`
  - `optional_env(name: str) -> str | None`: stripped value, `None` when unset or blank.
  - `env_flag(name: str, default: bool = False) -> bool`: accepts `true/false/1/0/yes/no/on/off` case-insensitively; anything else raises `ConfigurationError` naming the variable.
  - `missing_required_settings() -> list[str]`: names of required variables that are unset. Always required: `DB_SERVER`, `DB_USERNAME`, `DB_DATABASE`, `PGDB_PW`, `CRUCIBLE_SECRET_KEY`. When `INGESTION_ENABLED`: `RMQ_HOST`, `RMQ_USER`, `RABBITMQ_DEFAULT_PW`. When `PRINTING_ENABLED`: `MQTT_HOST`, `MQTT_USERNAME`, `MQTT_PASSWORD`. When `PROPOSAL_SYNC_ENABLED`: `PROPOSAL_DB_URL`, `PROPDB_APIKEY`. A secret counts as set when either `<NAME>` or `<NAME>_VERSION` is set.
  - `check_settings() -> None`: raises one `ConfigurationError` listing every missing name, sorted.
- Produces in `app/general_utils.py`: `get_secret(name: str) -> str`. Order: `<NAME>` env value if set; else `<NAME>_VERSION`, which must be a full `projects/.../secrets/.../versions/...` path, read through the existing `get_gcs_secret` and cached in `os.environ[name]` as today; else `ConfigurationError(f"{name} is not set")`. No hard-coded Secret Manager paths remain.

- [ ] **Step 1: Write the failing tests** in `tests/test_config.py`:
  - `test_blank_values_are_unset`: `RECORD_BUCKET="  "` gives `optional_env("RECORD_BUCKET") is None`.
  - `test_env_flag_parses_and_rejects_typos`: `"TRUE"`, `"1"`, `"on"` give `True`; unset gives the default; `"ture"` raises `ConfigurationError` whose message contains `PRINTING_ENABLED`.
  - `test_missing_required_lists_all_names`: with only `DB_SERVER` set, `missing_required_settings()` contains `PGDB_PW` and `CRUCIBLE_SECRET_KEY`; with `INGESTION_ENABLED=true` it also contains `RMQ_HOST`.
  - `test_version_reference_satisfies_secret`: `PGDB_PW` unset and `PGDB_PW_VERSION` set means `PGDB_PW` is not missing.
  - `test_get_secret_prefers_env_then_version_then_errors`: env value returned without calling Secret Manager (monkeypatch `get_gcs_secret` to fail if called); with only `_VERSION`, the patched `get_gcs_secret` receives the exact path; with neither, `ConfigurationError`.
  - `test_get_secret_rejects_relative_version`: `PGDB_PW_VERSION="pgcloud_pw/versions/2"` raises `ConfigurationError`.
- [ ] **Step 2:** Run `pytest tests/test_config.py -v`; expect failures (module missing).
- [ ] **Step 3:** Implement `app/config.py` and the new `get_secret`; update every caller to `get_secret("<NAME>")`; remove the module-level `rmq_pw` in `rmq_utils.py` (callers already pass the password); delete `local_sql_utils.py`.
- [ ] **Step 4:** At the top of `tests/conftest.py`, before importing `app`, `os.environ.setdefault` dummy values for `DB_SERVER`, `DB_USERNAME`, `DB_DATABASE`, `PGDB_PW`, `CRUCIBLE_SECRET_KEY`, so the suite no longer depends on a developer `.env`. Verify with `env -i HOME=$HOME PATH=$PATH .venv/bin/python -m pytest -q` from a directory with no `.env` above it (or temporarily rename the parent `.env`).
- [ ] **Step 5:** Full suite passes. Commit: `Add explicit settings and secret references`.

### Task 3: Feature flags and Foundry defaults

**Files:**
- Modify: `app/utils/constants.py` (RMQ settings without defaults), `app/utils/rmq_utils.py`
- Modify: `app/api/routes/print.py` (broker settings, flag)
- Modify: `app/utils/user_utils.py` (`MF_PROPOSAL_DB_URL` from `PROPOSAL_DB_URL`, flag)
- Modify: `app/api/routes/files.py` (ingest route near line 215), `app/api/routes/pipelines.py` (flag)
- Modify: `app/services/storage_downloads.py` (`DEFAULT_UPLOAD_BUCKET` removed), `app/api/routes/gcs_storage.py` (`GCS_PROJECT` without default)
- Modify: `app/api/main.py` (exception handlers)
- Test: `tests/test_feature_flags.py`

**Interfaces:**
- Consumes: `env_flag`, `optional_env` from Task 2.
- Produces:
  - `class FeatureDisabled(Exception)` with attribute `feature: str`, in `app/config.py`; `require_feature(flag: str, feature: str) -> None` raises it when `env_flag(flag)` is false. Flags: `PRINTING_ENABLED` / `"printing"`, `INGESTION_ENABLED` / `"ingestion"` (ingestion and postprocessing), `PROPOSAL_SYNC_ENABLED` (no route; `find_mf_user_proposals` returns `[]` without any network call when false).
  - `class StorageNotConfigured(Exception)` in `app/services/storage_downloads.py`; `upload_bucket()` raises it when `UPLOAD_BUCKET` is unset.
  - Exception handlers in `main.py`: `FeatureDisabled` gives `503` `{"detail": {"code": "feature_disabled", "feature": ..., "message": ...}}`; `StorageNotConfigured` gives `503` `{"detail": {"code": "storage_not_configured", "message": ...}}`.
  - Document the `503` responses on the affected operations (`responses=`) and regenerate `openapi/`.

- [ ] **Step 1: Write the failing tests** in `tests/test_feature_flags.py`:
  - `test_print_routes_disabled`: with `PRINTING_ENABLED` unset, `GET /print/printers` and `POST /print/barcode` return `503` with `code == "feature_disabled"` and `feature == "printing"`, and no MQTT client is created (monkeypatch the client class to fail if constructed).
  - `test_ingest_disabled_creates_no_request`: with `INGESTION_ENABLED` unset, the ingest route returns `503` `feature_disabled` and the `IngestionRequest` table is empty.
  - `test_ingest_broker_failure_marks_request_failed`: with `INGESTION_ENABLED=true` and `send_pika_message` raising `pika.exceptions.AMQPConnectionError`, the route returns `503` and the created `IngestionRequest` has status `failed`.
  - `test_postprocess_disabled`: `POST /datasets/{mfid}/postprocess` returns `503` `feature_disabled` and creates no `PostprocessingRun`.
  - `test_proposal_sync_disabled_makes_no_request`: with `PROPOSAL_SYNC_ENABLED` unset, `find_mf_user_proposals("0000-0001-0000-0001") == []` and `requests.get` is not called.
  - `test_download_without_bucket`: with `UPLOAD_BUCKET` unset, a download-link request for a stored file returns `503` `storage_not_configured`.
- [ ] **Step 2:** Run them; expect failures.
- [ ] **Step 3:** Implement. RMQ, MQTT, and proposal values are read when used, not at import. `files.py` uses `RMQ_USER` from settings instead of the literal `"admin"`. Existing tests that exercise printing, ingestion, or postprocessing set the relevant flag to `true` in their fixtures.
- [ ] **Step 4:** Full suite passes; `openapi/` regenerated. CHANGELOG `Changed`: settings are explicit, Foundry defaults removed, `503 feature_disabled` and `503 storage_not_configured`. Commit: `Make printing, ingestion, proposal sync and storage explicit settings`.

### Task 4: Startup check, API address settings, container port

**Files:**
- Modify: `app/api/dependencies.py` (`get_api_config`, `get_api_url`, `get_api_path`, `get_trgm_search_threshold`)
- Modify: `app/api/main.py` (call `check_settings()` before building the app)
- Modify: `Dockerfile` (port and workers from the environment)
- Test: `tests/test_api_settings.py`

**Interfaces:**
- Produces: `API_CONF_FILE` becomes optional. Environment values win over the YAML: `API_PUBLIC_URL` (scheme and host, for example `http://localhost:8080`), `API_PATH` (for example `api/v3`), `TRGM_SEARCH_THRESHOLD` (default `0.3`), `ORCID_CLIENT_ID` (already env-first). `get_api_url()` returns `API_PUBLIC_URL` without trailing slash plus `get_api_root_path()`; without `API_PUBLIC_URL` it keeps today's `https://{hostname}` from the YAML. With neither, it raises `ConfigurationError` naming `API_PUBLIC_URL` when called.
- Dockerfile: no `BRANCH_NAME` port file; `CMD` binds `0.0.0.0:${PORT:-8000}`; worker count from gunicorn's standard `WEB_CONCURRENCY` (default 4 when unset).

- [ ] **Step 1: Write the failing tests:** `test_public_url_env_wins_and_is_normalized` (`API_PUBLIC_URL="http://lab.local:8080/"`, `API_PATH="/api/v3/"` gives `get_api_url() == "http://lab.local:8080/api/v3"`); `test_yaml_still_works` (only `API_CONF_FILE` pointing at a temp YAML gives today's `https://` URL); `test_no_config_file_is_fine_until_url_needed` (no `API_CONF_FILE`, `get_api_root_path() == ""`, `get_api_url()` raises `ConfigurationError`); `test_startup_fails_listing_missing_settings` (importing a fresh app module with `CRUCIBLE_SECRET_KEY` unset raises `ConfigurationError` mentioning it; use `importlib` in a subprocess so the shared app is untouched).
- [ ] **Step 2:** Run; expect failures. **Step 3:** Implement; clear the `lru_cache` of `get_api_config` in tests.
- [ ] **Step 4:** `docker build -t crucible-api:selfhost .` then `docker run --rm -e PORT=8123 ...` with the minimum env and a reachable database starts gunicorn on 8123 (check the log line). Foundry deployments already set `PORT` (Task 1).
- [ ] **Step 5:** Full suite passes. Commit: `Read API address and port from the environment and check settings at startup`.

### Task 5: Baseline schema and the `db upgrade` command

**Files:**
- Create: `migrations/schema/baseline_b8f2d6e41c07.sql`
- Create: `app/cli/__init__.py`, `app/cli/db.py`
- Create: `scripts/compare_schema.sh`
- Modify: `migrations/env.py` (`PRODUCTION_DB` from `ALEMBIC_PRODUCTION_DATABASE`, default `"crucible"`; blank disables the guard)
- Modify: `Dockerfile` (copy `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/schema/`)
- Modify: `migrations/README.md` (fresh installs, baseline, schema comparison)
- Test: `tests/test_db_upgrade.py` (Postgres-gated with `CRUCIBLE_TEST_PG_URL`, using `tests/pg_fixture.py` conventions)

**Interfaces:**
- Produces in `app/cli/db.py`:
  - `BASELINE_REVISION = "b8f2d6e41c07"`
  - `upgrade_database(engine, alembic_ini: str = "alembic.ini") -> str`: returns `"initialized"` when the `public` schema had no tables (baseline applied, then `upgrade head`), `"upgraded"` when `alembic_version` exists (`upgrade head` only). Raises `RuntimeError` when tables exist without `alembic_version`. Applying the baseline and writing `alembic_version` happen in one transaction.
  - `python -m app.cli.db upgrade` calls it with `get_engine()` and prints the result.
- Produces `scripts/compare_schema.sh URL_A URL_B`: schema-only `pg_dump` of both with the `postgres:17` client image (`--schema-only --no-owner --no-privileges`), drops comment lines, blank lines, `SET` lines and `\restrict`/`\unrestrict` lines, diffs, exits `1` on any difference.

- [ ] **Step 1 (needs user approval for a read-only staging dump):** `pg_dump --schema-only --no-owner --no-privileges` of `crucible-staging` through the proxy with a Postgres 17 client. Confirm `alembic_version` is `b8f2d6e41c07`. Remove `\restrict`/`\unrestrict` lines. Add the seed rows the application needs, starting with the `public` access group (`PUBLIC_GROUP_NAME` in `app/utils/permissions.py`; copy its `group_type` from staging); grep `app/` for other rows looked up by fixed name and add those too. Do not include the `alembic_version` row; `upgrade_database` writes it.
- [ ] **Step 2: Write the failing tests** in `tests/test_db_upgrade.py`: `test_empty_database_is_initialized_to_head` (returns `"initialized"`, `alembic_version` equals `ScriptDirectory` head, the `public` group exists, `vector` and `pg_trgm` extensions exist); `test_second_run_is_upgrade_noop` (returns `"upgraded"`, head unchanged); `test_tables_without_revision_refused` (create one table, expect `RuntimeError`, table still the only one); `test_failure_midway_leaves_database_empty` (monkeypatch the version write to raise after the baseline SQL ran, expect the exception, then zero tables in `public`); `test_guard_name_configurable` (with `ALEMBIC_PRODUCTION_DATABASE=""` the guard does not fire on a database named `crucible`).
- [ ] **Step 3:** Run with `CRUCIBLE_TEST_PG_URL` against `pgvector/pgvector:pg17`; expect failures. **Step 4:** Implement.
- [ ] **Step 5 (needs the same approval):** Initialize a local `pgvector:pg17` database with `upgrade_database`, then `scripts/compare_schema.sh <local> <staging>`. Expected: no differences. Record the command in `migrations/README.md`.
- [ ] **Step 6:** Full suite and gated tests pass. Commit: `Add a schema baseline and db upgrade command for fresh installs`.

### Task 6: Admin bootstrap

**Files:**
- Modify: `app/utils/user_utils.py` (extract user provisioning), `app/api/routes/users.py` (`POST /users` uses it)
- Create: `app/cli/admin.py`
- Test: `tests/test_admin_bootstrap.py`

**Interfaces:**
- Produces in `app/utils/user_utils.py`: `provision_user(session, *, unique_id: str, first_name: str, last_name: str, email: str | None, username: str | None, project_groups: Sequence[AccessGroup] = (), platform_role: PlatformRole | None = None) -> User`. It performs exactly what `POST /users` does today inside its session (user row, identity group, owner link, contributor links to the given project groups), with uniqueness checks, and does not commit. `POST /users` keeps its responses byte-for-byte (existing tests prove it).
- Produces: `python -m app.cli.admin create-admin --first-name F --last-name L [--email E] [--username U] [--orcid O]`. Without `--orcid` the user gets an MFID (`get_mfid_value()`); with it, the value must pass `is_orcid`. Generates a username with `generate_unique_username` when none is given. Sets `platform_role` to `PlatformRole.ADMIN`, mints the API key with `find_or_add_api_access`, prints one JSON object `{"unique_id": ..., "username": ..., "api_key": ...}` to stdout. Exits `1` with a message when any user already has `platform_role == admin`, unless `--allow-additional` is passed.

- [ ] **Step 1: Write the failing tests** (SQLite `db_engine` fixture, call the CLI entry function directly): `test_create_admin_creates_user_group_and_key` (admin role, identity group owned by the user, one `ApiToken`, JSON on stdout); `test_second_admin_refused` (exit `1`, still one admin); `test_allow_additional` (second admin created); `test_orcid_validated` (`--orcid not-an-orcid` exits non-zero, nothing created); `test_post_users_unchanged` (existing `POST /users` tests still pass, no new test needed beyond running them).
- [ ] **Step 2:** Run; expect failures. **Step 3:** Implement. **Step 4:** Full suite passes. Commit: `Add an admin bootstrap command`.

### Task 7: Compose bundle and install guide

**Files:**
- Create: `deploy/compose/compose.yaml`, `deploy/compose/.env.example`, `deploy/compose/Caddyfile`, `deploy/compose/README.md` (install guide)
- Modify: `docs/README.md` (link the guide), `README.md` (one line pointing to it)

**Interfaces:**
- Consumes: settings from Tasks 2 to 4, `python -m app.cli.db upgrade` (Task 5), `python -m app.cli.admin create-admin` (Task 6).
- Produces services:
  - `db`: `pgvector/pgvector:pg17`, named volume, `pg_isready` healthcheck, `POSTGRES_DB=crucible`.
  - `migrate`: built from the repository root, runs `python -m app.cli.db upgrade`, `depends_on: db: condition: service_healthy`, `ALEMBIC_PRODUCTION_DATABASE=""`.
  - `api`: same image, `PORT=8000`, `API_PUBLIC_URL`, `API_PATH=api/v3`, all feature flags off, `HUB_AUTH_ENABLED=false`, `depends_on: migrate: condition: service_completed_successfully`, healthcheck on `/health/ready` with Python's `urllib` (the image has no curl).
  - `proxy`: `caddy:2` publishing `${CRUCIBLE_HTTP_PORT:-8080}:80`. `handle_path /api/v3/*` to `api:8000` (strips the prefix, as `crucible-nginx` does today). HTTPS is documented, not default.
- `.env.example` lists every variable with a one-line explanation; secrets are placeholders the guide tells you to generate (`openssl rand -hex 32`).
- Install guide covers: prerequisites (Docker Engine with Compose v2), configuration, `docker compose up -d --build --wait`, creating the administrator, checking `/api/v3/health/ready`, pointing nano-crucible at `http://<host>:8080/api/v3` with the API key, what is not available yet (file storage arrives in Phase 2, Hub sign-in needs ORCID until Phase 3), backups with `pg_dump`, and upgrading (rebuild and `up`; `migrate` runs automatically).

- [ ] **Step 1:** Write the files. **Step 2:** From a clean checkout: `cp .env.example .env`, fill secrets, `docker compose up -d --build --wait`; expected: all services healthy, `curl -s localhost:8080/api/v3/health/ready` shows `"status":"ok"` and `"schema_state":"current"`. **Step 3:** `docker compose run --rm api python -m app.cli.admin create-admin --first-name Ada --last-name Admin` prints a key; `curl -H "Authorization: Bearer <key>" localhost:8080/api/v3/account` returns the admin profile (use the auth header form the API documents). **Step 4:** `docker compose down -v`. Commit: `Add a docker compose bundle and install guide`.

### Task 8: Smoke test and CI

**Files:**
- Create: `scripts/selfhost_smoke.py`
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Produces `python scripts/selfhost_smoke.py --api-url URL --api-key KEY`: checks health (`status == "ok"`, `schema_state == "current"`), creates a project, a sample and a dataset in it, links them, reads the dataset back by MFID and through `GET /datasets?mfid=`, and confirms an unauthenticated request is refused. Exits `0` on success, non-zero with the failing step named.
- Produces workflow `ci.yml` on pull requests to `staging` and `main`, three jobs:
  - `tests`: `astral-sh/setup-uv`, `uv sync --locked`, `uv run pytest -q` with no `.env`.
  - `postgres`: service `pgvector/pgvector:pg17`, runs the gated tests (`CRUCIBLE_TEST_PG_URL`), including Task 5's.
  - `compose-smoke`: generates `.env` from `.env.example` with random secrets, `docker compose up -d --build --wait`, `create-admin`, runs the smoke script against the proxy, prints `docker compose logs` on failure, `down -v` always.

- [ ] **Step 1:** Write the smoke script; run it against Task 7's stack locally; expected exit `0`. Break one step on purpose (wrong key); expected non-zero and the step named.
- [ ] **Step 2:** Write the workflow; push the branch and open the PR; all three jobs pass. Commit: `Add CI with a self-hosted smoke test`.

### Task 9: Hub in the compose bundle (depends on crucible-hub)

**Files:**
- Modify: `deploy/compose/compose.yaml`, `deploy/compose/.env.example`, `deploy/compose/Caddyfile`, `deploy/compose/README.md`

**Interfaces:**
- Consumes from the crucible-hub session (request by message, do not edit the hub repository): (a) accepting a plain-HTTP internal API URL such as `http://api:8000` when explicitly configured (today `CRUCIBLE_API_BASE_URL` must be HTTPS or localhost); (b) how its session-store migrations run in a container; (c) the image to use (a release tag to build from `https://github.com/MolecularFoundryCrucible/crucible-hub.git#<tag>`, or a published image).
- Produces: `hub` service with `HUB_DATA_SOURCE=hub-auth`, sessions in a `hub` database on the same Postgres, Caddy `handle /hub*` to `hub:3000` without stripping. ORCID sign-in configuration documented as optional in the guide; until Phase 3 the Hub cannot sign anyone in without it.

- [ ] **Step 1:** Message `crucible-hub` with (a) to (c). Wait for answers; record them in this task.
- [ ] **Step 2:** Add the service; `docker compose up -d --build --wait`; `curl -s localhost:8080/hub/health` returns `200`. With ORCID configured (manual check by the user), sign-in reaches the Hub home page.
- [ ] **Step 3:** CI `compose-smoke` also checks `/hub/health`. Commit: `Add the Hub to the compose bundle`.

### Task 10: Close out Phase 1

**Files:**
- Modify: `CHANGELOG.md`, `docs/self_hosting_design.md` (status line), `migrations/README.md`, `docs/README.md`

- [ ] **Step 1:** CHANGELOG `Added`: compose bundle, `db upgrade`, `create-admin`, CI. `Changed`: settings and secrets, feature flags, port. Operator note: new required settings for any non-Foundry deployment.
- [ ] **Step 2:** Design doc status: Phase 1 complete, with the PR numbers. Commit: `Record Phase 1 completion`.
