# Self-hosted Crucible: design

Status: agreed direction 2026-10-22. Work happens in small pull requests to `staging`, starting with Phase 1. The core comes first; optional plug-ins (ingestion, printing, telemetry) follow in a second stage.

## Why

Crucible today runs only as the Molecular Foundry's deployment on Google Cloud. It should be research data infrastructure that a small group can install and run on their own machines, without proprietary software, and that can later move to the cloud when funding allows. The Foundry's deployment becomes one installation among others, and keeping it running should get easier, not harder.

## Who it is for

An independent researcher or small group with a couple of workstations and no cloud account. One person installs Crucible, becomes platform administrator, creates a project, and invites the others, who register and sign in. During the first week they record samples and datasets with nano-crucible and the uploader UIs, pointed at their own Crucible. They host their own database, API, and storage.

Larger institutions run the same software on a public address, as the Foundry does today.

## Goals

- `docker compose up` on one workstation gives a working Crucible: Postgres, the API, the Hub, and S3-compatible object storage.
- Moving to the cloud later means pointing the same setup at managed Postgres and managed object storage, not rebuilding it.
- Only open-source software is required.
- One codebase and one way of doing things for every installation, the Foundry included.
- The Foundry deployment keeps working throughout, and is easier to operate afterwards.

## Non-goals

- The graph explorer. It is being abandoned and is not ported.
- Label printing and instrument telemetry in the core install. They become optional plug-ins (see `printer_registry_followup.md`).
- The Foundry proposal database integration (`foundry-admin.lbl.gov` project auto-assignment) for self-hosters. It stays a Foundry-only option, off by default.
- HPC cluster execution and Globus transfers in the first phases. The design must leave room for them; they are built later.
- Moving the Foundry's data out of Google Cloud Storage.
- Building password handling into Crucible. Passwords live in an identity provider (see Sign-in).

## Anti-goals

These make the effort a failure even if every piece works:

- An install that needs a Google account or any vendor service to start.
- Setup steps or configuration that only make sense to the Crucible team.
- Two diverging paths, one for the Foundry and one for everyone else, that must be kept in sync. No fork.
- Any change that breaks or degrades the running Foundry deployment.

## Deployment tiers

The same images and settings serve both tiers; the tier decides networking, TLS, and sign-in.

- **Lab tier.** One or two workstations, reachable on the local network. Compose includes everything, including an identity provider. TLS is optional; the install guide covers a local certificate or a reverse proxy.
- **Institution tier.** A public address with HTTPS, as the Foundry runs today. Managed or institutional Postgres, object storage, and sign-in may replace the bundled services.

Every address a browser or client must reach (the API, the Hub, presigned storage URLs, the sign-in provider) is configurable as a public URL separate from the internal compose address, because presigned URL signatures cover the host.

## Decisions

### Core install

- Services: Postgres (with the `vector` and `pg_trgm` extensions), the API, the Hub, and an S3-compatible object store; for the lab tier also an OIDC identity provider. Message brokers belong to the plug-ins, not the core.
- The object store must be open source. MinIO removed features from its free edition and stopped community binary releases in 2025, so compare Garage, SeaweedFS, and MinIO before choosing (builder's call, recorded in the PR).
- The installer becomes platform administrator. There is no admin bootstrap today; one is needed.
- The Hub talks to the API by URL and stores sessions in plain Postgres or Redis. `HUB_SESSION_DATABASE_URL` and `HUB_SESSION_REDIS_URL` already exist in crucible-hub; its Cloud SQL connector is a Foundry option. Phase 1 can use the current Hub image.

### Sign-in

- The API becomes a standard OpenID Connect (OIDC) relying party: it signs users in through a configured provider and stores the provider's subject identifier, never a password.
- ORCID is an OIDC provider, so it becomes one configured provider instead of a special case. The Foundry keeps ORCID; registering your own ORCID client stays a documented step for installs on a public address.
- Lab tier: compose bundles an open-source identity provider that handles registration, passwords, reset, two-factor, and brute-force protection, and can federate ORCID or an institution's login. Candidates with self-registration: Keycloak (Apache-2.0, heavy), Zitadel (AGPL-3.0), Authentik (MIT core, some paid enterprise features). Dex and Authelia are lighter but have no self-registration. Choose by footprint on a two-workstation install (builder's call, recorded in the PR).
- Institution tier: point the API at ORCID or the institution's own OIDC/SAML-backed provider.
- The Hub's sign-in flow with the API (`/auth/hub/*`) is unchanged; only what the API does behind its login changes.
- A user who signs in without ORCID gets an MFID in the user identifier column, as service accounts already do. Separating identity from ORCID for everyone is the planned user-MFID work in `mfid_naming_transition.md`, not part of this project. Linking several providers to one account is out of scope at first (builder may add it).

### Storage

- One S3-style storage interface for all byte operations: upload (multipart with presigned part URLs), download (presigned GET), replace, delete, and record archiving.
- Configuration chooses the endpoint and credentials. The Foundry keeps its data in Google Cloud Storage and reaches it through GCS's S3-compatible XML API with HMAC keys; self-hosters use their own S3-compatible store or a cloud provider's.
- Existing Foundry objects do not move: same buckets, same `bucket/key` paths, no data migration.
- The legacy upload routes (`POST /datasets/{dataset_mfid}/upload/initiate`, `/upload/initiate/multipart` with its downscoped Google token, and `/upload/complete`) are retired through the normal deprecation process once nano-crucible and other clients use the presigned multipart routes. nano-crucible drops its `google-cloud-storage` dependency at that point.
- GCS-only behavior is replaced by something every S3-style store supports where possible, and retired otherwise if it is not important. Known cases: generation pinning on downloads (S3 version IDs, or ETag checks), retiring replaced objects through `customTime` and a GCS lifecycle rule (explicit delete or a sweeper), provider-computed full-object CRC32C (S3 checksum modes differ), and downscoped tokens (no equivalent; retired with the legacy routes).
- The existing multipart adapter (`app/services/multipart_storage.py`, `docs/multipart_storage_backends.md`) is the starting point.
- One adapter may carry small per-provider differences (for example, how GCS reports checksums or versions) behind capability flags. That is not the "two paths" anti-goal, which is about parallel workflows and features that exist on only one deployment. (Design call.)
- Integrity does not depend on the store: the API verifies a checksum on upload completion. Bucket versioning is a recommended safety net, not a requirement, so stores without it (Garage) remain eligible; the Foundry keeps its GCS versioning and retention. (Design call.)
- `storage_backend` is a public field that defaults to `"gcs"` and means "has bytes in Crucible storage". It gets a provider-neutral value for "stored in the configured object store"; `"gcs"` stays accepted as a deprecated alias on input, and existing Foundry rows are rewritten in a later data migration through the usual deprecation process. Constraints and indexes stop using the `'gcs'` literal. (Design call.)
- The browser uploads directly to the object store, so the store must allow cross-origin `PUT` from the Hub's origin and expose `ETag`.

### Plug-ins (second stage)

Crucible uses two open-source brokers for different jobs; neither is part of the core.

- **Ingestion and post-processing** use RabbitMQ (MPL-2.0) as a job queue from the API to consumers.
  - Ingestors stay separate services that receive work from the API and write results back through the API, as today.
  - File access is configurable: read directly from where the bytes are (a shared directory or the bucket itself, so nothing is copied), or download through API download links when running somewhere without that access.
  - The broker setup works out of the box (declared queues, configurable user and routing suffix). With no broker configured, ingestion is disabled cleanly instead of leaving an orphaned request (today `POST` ingest commits the request and then fails with 500).
  - Self-hosters add their own parsers without forking `crucible-ingestion`: an entry-point plugin hook like the one `crucible-parsers` already uses on the client side.
  - Code: `crucible-ingestion` (parsers), `ingestion-cloud-consumer` (ingestion consumer), `crucible-rabbit-mq` (post-processing consumers; currently in the `MolecularFoundry` GitHub organization, to move to `MolecularFoundryCrucible`), `mosaic-stitch-consumer`.
- **Printing and telemetry** use MQTT through Eclipse Mosquitto (EPL/EDL): label print commands and printer status (`crucible-label-printer`, `crucible-label-printer-status`), instrument and sensor telemetry over the CAST protocol (`crucible-cast`, `crucible-mqtt-sensing`), and broker user tooling (`crucible-mqtt-broker`, which today stores passwords in Google Secret Manager). Broker host, credentials, and TLS come from settings; printing is off by default.
- RabbitMQ also speaks MQTT through a plug-in, so a small install could run one broker for both. To evaluate when the plug-ins are built.

### Later: HPC and Globus

Not built now. The design should not block: a file existing in more than one location, jobs that record where they ran, and results produced on nodes without API access and pushed later (the ingestion parse/push split already allows this).

## Licensing, repositories, and releases

- Every repository needed to run Crucible gets an open-source license. Software written at Berkeley Lab needs approval from the Lab's Intellectual Property Office before an open-source release (and DOE-funded software is reported to DOE CODE), so that request starts early; it sets the timeline for publishing. The Lab usually releases under a BSD-style license. Apache-2.0 or BSD-3-Clause are the usual permissive choices; AGPL-3.0 if modified hosted versions must publish their changes. The choice is the project lead's, after the IPO review.
- Public repositories: `crucible-api` (private today), `crucible-hub`, `nano-crucible`, `crucible-ingestion`, and the consumer, printing, and MQTT repositories. Private infrastructure repositories (`crucible-nginx`, `crucible-ecosystem`) stay private unless they are needed to run Crucible.
- Releases follow standard practice: container images built by GitHub Actions on version tags and published to GitHub Container Registry (`ghcr.io/molecularfoundrycrucible/<name>`), with semantic version tags (`1.4.0`, `1.4`), for amd64 and arm64. A `compose.yaml`, `.env.example`, and install guide are published with each release. Database migrations run as a one-shot step on upgrade, with a documented upgrade path from each release to the next. nano-crucible stays on PyPI. A Helm chart for Kubernetes institutions can come later.
- The Foundry deploys the same released images; its Cloud Build pipeline can remain for its own deploy step.

## Phases

Each phase ships on its own and is tested before the next. Phases 1 to 4 are the core; Phase 5 is the plug-in stage.

1. **A fresh install works.**
   - A real baseline migration: today the Alembic baseline is empty and the schema came from archived SQL, so a new database cannot reach head. The baseline creates the extensions and the schema and only runs on an empty database; existing databases are untouched.
   - Explicit settings replace scattered defaults: Secret Manager project `776258882599`, RabbitMQ `10.128.0.31`, `mf-storage-prod` / `crucible-prod`, `mqtt.mfdata.org`, `foundry-admin.lbl.gov`, the branch-dependent port in the Dockerfile, and the root-HOME credential assumption. Missing secrets fail loudly instead of falling back to Secret Manager; Secret Manager becomes an explicit opt-in.
   - Optional features can be switched off: printing, the proposal integration, ingestion publishing, record archiving.
   - An admin bootstrap.
   - A `docker compose` setup with Postgres, the API, and the Hub (built from source until releases exist), plus a short install guide.
2. **One S3-style storage layer** as described above, the object store added to compose, and the legacy upload routes deprecated.
3. **OIDC sign-in**, with ORCID as a configured provider and a bundled identity provider in the lab-tier compose.
4. **Releases**: licenses, public repositories, published images, versioned compose bundle, upgrade path. The license review can start immediately and run alongside Phases 1 to 3.
5. **Plug-ins**: ingestion and post-processing (RabbitMQ), then printing and telemetry (MQTT).
6. **Later:** HPC execution, Globus.

## Not breaking the Foundry deployment

1. Pin first, then change defaults. Every value the code assumes today goes into the Foundry config (`k8s-conf` ConfigMaps) and is deployed before the code default changes.
2. The baseline migration only runs on an empty database. A check builds a fresh database from migrations and compares its schema with a schema-only dump of staging; any difference fails.
3. Storage switches by setting, one environment at a time. The current GCS adapter stays until the S3-style path is proven. Staging switches first and runs the `crucible-tests` end-to-end suite against the real staging bucket through the S3-compatible API; production switches only after that, and switching back is a config change.
4. Sign-in switches the same way: the current ORCID flow stays until the OIDC path, configured with ORCID, passes on staging. Existing users keep their ORCID identifiers.
5. The HTTP contract changes only on purpose: the OpenAPI snapshot diff in each PR, deprecation headers and client coordination before removing routes, `crucible-tests` on staging before every merge to `main`, rolling staging-first deploys.
6. A CI job starts the compose setup on every PR and runs a smoke test (create a project, upload a file, download it), so Foundry changes cannot silently break self-hosting and the other way round.

## Clients

- crucible-hub: needs only its API URL and a session store; make the footer, QR trusted host, and Foundry branding configurable.
- nano-crucible: change the default URL and setup text, make Google libraries optional, and use presigned multipart only once the legacy routes are retired.
- crucible-upload-uis: read the API URL from config instead of hard-coding `https://crucible.lbl.gov/api/v3`, make printing optional, and ship a generic instrument template.
- Client changes go through the nano-crucible and crucible-hub sessions, not edits from this repo. Who changes the plug-in repositories is decided when Phase 5 starts.

## Left to the builder

- Choice of object store and identity provider for compose, and of Python S3 and OIDC client libraries.
- How settings are structured (one settings object, env var names), as long as they are documented and fail loudly when required values are missing.
- How the baseline migration is produced (squash, `create_all` plus stamp, or generated SQL), as long as the schema comparison passes.
- How the admin bootstrap works (first sign-in, a CLI command, or an env var naming the administrator).
- The order of pull requests inside a phase.
