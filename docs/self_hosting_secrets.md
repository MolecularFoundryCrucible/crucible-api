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

## Task 6b

- `app/config.py`: `NAME_FILE` support and the ambiguity check in `get_secret` and the startup check; the Hub and cursor secrets as `Settings` fields; Hub secrets in the startup check when Hub sign-in is on.
- `app/utils/hub_secrets.py`: read through `get_secret`; no module-level vendor import. Its current checks (5 second timeout on Secret Manager, rejecting values with line breaks) are kept.
- `app/utils/cursor_utils.py`: read the cursor keys through settings.
- Tests: each way works; two ways for one secret is refused; `NAME_FILE` pointing to a missing or empty file is refused; Hub secrets from a file; importing the application does not import the Secret Manager client; the Foundry ConfigMaps pass the startup check (extend `tests/test_k8s_pinned_settings.py`).
- Docs: the inventory and recipes in the install guide; CHANGELOG.

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
