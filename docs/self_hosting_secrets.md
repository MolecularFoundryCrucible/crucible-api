# Secrets: design

Status: proposed 2026-10-26, for approval. Implements as Phase 1 Task 6b, before the compose bundle (Task 7).

## Goal

One vendor-neutral way for the API to receive every secret, so that a small lab (docker compose on one machine), a bare-metal install (systemd), a Kubernetes cluster with any secrets manager, and the Foundry (GKE) all work without code changes and without vendor clients in the core image. Delivery is the platform's job; the application only reads what it is handed.

## Today (review, 2026-10-26)

| Code | Secrets | Sources, in order | Problems |
|---|---|---|---|
| `app/config.py` `get_secret()` | `PGDB_PW`, `CRUCIBLE_SECRET_KEY`, `RABBITMQ_DEFAULT_PW`, `MQTT_PASSWORD`, `PROPDB_APIKEY`, `GCS_SA`, `ORCID_CLIENT_SECRET` | environment variable, file named after the secret in `CRUCIBLE_SECRETS_DIR` (default `/run/secrets`), GCP Secret Manager path in `NAME_VERSION` | sound |
| `app/utils/hub_secrets.py` `get_hub_secret()` | `HUB_AUTH_CLIENT_SECRET`, `HUB_AUTH_ORCID_CLIENT_SECRET` | `NAME_VERSION` first, then environment variable | no file support; opposite order; its own Secret Manager client imported at module load; not in the startup check |
| `app/utils/cursor_utils.py` | `CRUCIBLE_CURSOR_SIGNING_KEY`, `CRUCIBLE_CURSOR_SIGNING_KEYS` | raw environment variables | no file support; not in `Settings` |

Also: a secret can only be a file with exactly its name in one directory (no file at another path), and a secret given two ways is resolved silently by precedence. Values are already kept out of logs (`SecretStr`), trimmed, matched case-insensitively as file names, and checked at startup.

## Practice

The application does not fetch its own secrets; the platform delivers them, most often as files.

- Kubernetes points to the Secrets Store CSI Driver, which mounts secrets from Vault, AWS, Azure, GCP, CyberArk, Akeyless, and OpenBao as files. The External Secrets Operator syncs the same stores into Kubernetes Secrets.
- On GKE, Google recommends the Secret Manager add-on (that CSI driver) whenever the application reads files.
- Docker and Compose mount secrets at `/run/secrets/<name>`; official images such as Postgres also accept `NAME_FILE=/path`.
- systemd delivers credentials as files in `$CREDENTIALS_DIRECTORY` (`LoadCredential=`), not inherited by child processes and kept out of swap.
- Vault Agent and similar sidecars fetch secrets and write files.
- Environment variables remain universal (twelve-factor) and are the only option on some platforms, but OWASP ranks them least preferred for secrets: they leak into child processes, logs, and crash reports.
- When an application must fetch itself, pydantic-settings already provides optional AWS, Azure, and GCP sources; projects should not hand-roll clients.

## Design

### One contract

Every secret is a `SecretStr` field in `Settings` and is read only through `get_secret(NAME)`. A secret can be given in exactly one of these ways:

| Way | Typical use |
|---|---|
| `NAME_FILE=/path/to/file` | any tool that writes a file at its own path: systemd (`$CREDENTIALS_DIRECTORY/...`), Vault Agent, a CSI volume at a custom path |
| a file `NAME` in `CRUCIBLE_SECRETS_DIR` (default `/run/secrets` when it exists) | Compose and Swarm secrets, Kubernetes Secret volumes, CSI driver, GKE add-on |
| `NAME` environment variable | development; platforms that inject only environment variables (ECS, Cloud Run, Kubernetes `secretKeyRef`) |
| `NAME_VERSION=projects/.../versions/...` | transitional, Foundry only (see below) |

### Rules

1. A secret given more than one way stops the server at startup with the secret's name and the ways found, never its value. No silent precedence.
2. The application never generates a secret. A missing required secret stops the server with the full list of what is missing.
3. Secrets are read once and kept in memory, hidden from logs and error messages. Rotation is a restart (a rolling update on platforms that have one).
4. Keys that sign things accept a list of keys so rotation has no downtime: cursors already do (`CRUCIBLE_CURSOR_SIGNING_KEYS`); the session cookie follows through `docs/secret_key_separation_followup.md`.
5. One startup check covers every secret, including the Hub secrets when `HUB_AUTH_ENABLED=true`.
6. No vendor client is imported at module load. `NAME_VERSION` keeps working for the Foundry, documented as transitional; its client is imported only when a `NAME_VERSION` is used.
7. Files are read as UTF-8 text with surrounding whitespace removed; a file that is empty after trimming counts as missing.

### Inventory

| Secret | Required when | Notes |
|---|---|---|
| `PGDB_PW` | always | |
| `CRUCIBLE_SECRET_KEY` | always | signs session cookies, and cursors unless a cursor key is set |
| `RABBITMQ_DEFAULT_PW` | `INGESTION_ENABLED=true` | |
| `MQTT_PASSWORD` | `PRINTING_ENABLED=true` | |
| `PROPDB_APIKEY` | `PROPOSAL_SYNC_ENABLED=true` | |
| `GCS_SA` | file storage configured | service account key file; replaced by S3-style credentials in Phase 2 |
| `ORCID_CLIENT_SECRET` | ORCID sign-in through the API | checked when an ORCID sign-in route is used, as today |
| `HUB_AUTH_CLIENT_SECRET`, `HUB_AUTH_ORCID_CLIENT_SECRET` | `HUB_AUTH_ENABLED=true` | moved from `hub_secrets.py` into `Settings` |
| `CRUCIBLE_CURSOR_SIGNING_KEY` or `CRUCIBLE_CURSOR_SIGNING_KEYS` | optional | the second is a JSON object of key id to secret, for rotation |

### Delivery recipes (install guide)

Short, because the application side is always the same:

- Compose (bundle default): `secrets:` entries backed by files created with `openssl rand -hex 32` and `chmod 600`, in a git-ignored `secrets/` folder; Postgres reads the same password through `POSTGRES_PASSWORD_FILE`.
- systemd: `LoadCredential=PGDB_PW:/etc/crucible/pgdb_pw` and `CRUCIBLE_SECRETS_DIR=%d` (the credentials directory).
- Kubernetes: a Secret mounted as a volume at `/run/secrets`; or the Secrets Store CSI Driver or External Secrets Operator for an external manager.
- Vault Agent or similar: render each secret to a file and set `NAME_FILE` or `CRUCIBLE_SECRETS_DIR`.

### Horizontal scaling

All replicas must hold the same secrets, or a cookie or cursor issued by one replica fails on another; platform delivery gives every replica the same files, and rule 2 forbids per-instance generated secrets. Fetching from a secrets manager inside each replica multiplies API calls and startup time and meets quota limits as replicas grow, which is another reason to deliver files. Rotation during a rolling update relies on rule 4. Scaling concerns unrelated to secrets (database connections per replica, per-process caches, singleton jobs) need their own review.

### The Foundry

Phase 1 changes nothing for the Foundry: it keeps its `NAME_VERSION` references, and its ConfigMaps never give a secret two ways (checked 2026-10-26: only `_VERSION` entries for `ORCID_CLIENT_SECRET`, `HUB_AUTH_CLIENT_SECRET`, `HUB_AUTH_ORCID_CLIENT_SECRET`, and the others). After the Phase 1 release, a Foundry deploy repository step moves the deployment configuration to a private repository, tags images by version, and switches the Foundry to the GKE Secret Manager add-on (files); the in-app Secret Manager code and its dependency are then removed.

## Settings structure

Decided 2026-10-26: settings are grouped by area instead of one flat class. Today `Settings` has 30 flat fields, the per-feature requirements live in a side table (`_FEATURE_REQUIREMENTS`), and about 30 more values are read with `os.getenv` elsewhere (database pool, multipart limits, Hub sign-in, cursor keys, readiness, caches, thumbnails); by Phase 3 a flat class would pass 60 fields.

### Shape

- Each area is a small pydantic-settings class (`SettingsGroup` subclass) with its own environment prefix. Environment variable names do not change; names that do not follow the prefix (`PGDB_PW`, `RABBITMQ_DEFAULT_PW`, `PROPDB_APIKEY`, `INGESTION_ENABLED`, ...) are kept with `validation_alias`.
- `Settings` is a plain container of groups, built and cached by `get_settings()`; code reads `get_settings().db.server`.
- `SettingsGroup` holds the shared options (case-insensitive names, empty values ignored, values trimmed) and replaces the default sources with one custom source, `SecretSources`, built on pydantic-settings' documented extension point (`settings_customise_sources`, `PydanticBaseSettingsSource`). It looks up every field by its exact environment variable name in the ways listed above and enforces the "one way only" rule. The built-in secrets-directory source is not used, because it applies the group prefix to file names and does not reach nested groups (pydantic-settings issues #508 and #154).
- Each group validates its own requirements (for example, printing on requires the MQTT host, username, and password). `check_settings()` collects every group's problems into one message.

| Group | Contents (environment names unchanged) |
|---|---|
| `db` | `DB_SERVER`, `DB_USERNAME`, `DB_DATABASE`, `PGDB_PW`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT`, `DB_POOL_RECYCLE`, `EXPECTED_DB_REVISIONS` |
| `security` | `CRUCIBLE_SECRET_KEY`, `CRUCIBLE_CURSOR_SIGNING_KEY`, `CRUCIBLE_CURSOR_SIGNING_KEYS`, `CRUCIBLE_CURSOR_ACTIVE_KEY_ID`, `AUTH_CACHE_TTL`, `AUTH_CACHE_MAXSIZE` |
| `api` | `API_PUBLIC_URL`, `API_PATH`, `TRGM_SEARCH_THRESHOLD`, `ORCID_CLIENT_ID`, `ORCID_CLIENT_SECRET`, `API_CONF_FILE` |
| `hub` | `HUB_AUTH_ENABLED`, `HUB_AUTH_CLIENT_ID`, `HUB_AUTH_CLIENT_SECRET`, `HUB_AUTH_ORCID_CLIENT_ID`, `HUB_AUTH_ORCID_CLIENT_SECRET`, `HUB_AUTH_ORCID_ISSUER`, `HUB_AUTH_CALLBACK_URI`, `HUB_AUTH_PUBLIC_API_URL` |
| `storage` | `UPLOAD_BUCKET`, `GCS_PROJECT`, `GCS_SA`, `STORAGE_READ_BUCKETS`, `RECORD_BUCKET`, `MAX_UPLOAD_BYTES`, the `MULTIPART_*` settings, `LEGACY_UPLOAD_TOKEN_DOWNSCOPE`, the `THUMBNAIL_*` limits |
| `ingestion` | `INGESTION_ENABLED`, `RMQ_HOST`, `RMQ_PORT`, `RMQ_USER`, `RMQ_ROUTING_SUFFIX`, `RABBITMQ_DEFAULT_PW` |
| `printing` | `PRINTING_ENABLED`, `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` |
| `proposals` | `PROPOSAL_SYNC_ENABLED`, `PROPOSAL_DB_URL`, `PROPDB_APIKEY` |

Phase 2 replaces or extends `storage`; Phase 3 adds an `auth` group for local passwords.

Stay outside `Settings`, because they locate the settings or describe the build rather than configure the server: `DB_CONF_FILE`, `CRUCIBLE_SECRETS_DIR`, `ALEMBIC_PRODUCTION_DATABASE` (migration tooling, where an empty value means "off"), and the build metadata `API_VERSION`, `GITHASH`, `BRANCH_NAME`.

Also left where they are by Task 6b (2026-10-26), each for a reason: the `MULTIPART_*` settings and `MAX_UPLOAD_BYTES` belong to the storage layer that Phase 2 replaces, and `MultipartStorageConfig` already validates them strictly; the thumbnail limits (`THUMBNAIL_*`) and the authentication cache sizes (`AUTH_CACHE_TTL`, `AUTH_CACHE_MAXSIZE`) are read when their modules load, and moving them into `Settings` would bring back settings reads at import time. They move with the Phase 2 storage work, or when those modules stop reading them at import.

### Alternatives considered

- Nested models with a delimiter (`DB__SERVER`), the style the pydantic-settings documentation shows: renames every variable and changes the Foundry configuration. Rejected.
- One flat class, organized with sections and a single validator: common and simple, but about 60 fields by Phase 3 with no visible grouping. Rejected.
- The third-party `pydantic-settings-file-envar` package for `NAME_FILE`: young, single maintainer, and without the "one way only" rule. Our source is about 40 lines with tests.

## Task 6b

Two commits, so the restructuring can be reviewed apart from the behavior change:

1. Group the settings with no behavior change: `SettingsGroup`, the groups above, and `SecretSources` doing today's lookups (environment variable, then the secrets directory, then `NAME_VERSION`); move the scattered `os.getenv` reads in; update the 16 callers of `get_settings()`. Existing tests, including `tests/test_k8s_pinned_settings.py` for the Foundry ConfigMaps, pass unchanged in meaning.
2. The secrets rules: `NAME_FILE`, the "one way only" check, Hub secrets from files and in the startup check, `hub_secrets.py` reading through `get_secret` with no module-level Secret Manager import (keeping its 5 second timeout and its rejection of values with line breaks), cursor keys through settings.

Tests: each way works; two ways for one secret is refused; `NAME_FILE` pointing to a missing or empty file is refused; Hub secrets from a file; each group's requirements; importing the application does not import the Secret Manager client; the Foundry ConfigMaps pass the startup check (extend `tests/test_k8s_pinned_settings.py`). Docs: the inventory and recipes in the install guide; CHANGELOG.

## Sources

- Kubernetes, Good practices for Kubernetes Secrets: https://kubernetes.io/docs/concepts/security/secrets-good-practices/
- Secrets Store CSI Driver: https://secrets-store-csi-driver.sigs.k8s.io/
- External Secrets Operator: https://external-secrets.io/latest/
- Google Cloud, Secret Manager add-on for GKE: https://docs.cloud.google.com/secret-manager/docs/secret-manager-managed-csi-component
- Docker, Secrets in Compose: https://docs.docker.com/compose/how-tos/use-secrets/
- Postgres official image (`_FILE`): https://hub.docker.com/_/postgres
- systemd, Credentials: https://systemd.io/CREDENTIALS/
- OWASP Secrets Management Cheat Sheet: https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html
- The Twelve-Factor App, Config: https://12factor.net/config
- pydantic-settings: https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/
