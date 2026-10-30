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
- A general identity system: self-registration, email-based password reset, multi-factor authentication, or a bundled identity provider such as Keycloak. Local password login stays deliberately small (see Sign-in).

## Anti-goals

These make the effort a failure even if every piece works:

- An install that needs a Google account or any vendor service to start.
- Setup steps or configuration that only make sense to the Crucible team.
- Two diverging paths, one for the Foundry and one for everyone else, that must be kept in sync. No fork.
- Any change that breaks or degrades the running Foundry deployment.

## Deployment tiers

The same images and settings serve both tiers; the tier decides networking, TLS, and sign-in.

- **Lab tier.** One or two workstations, reachable on the local network, a few users. Compose includes everything. Users sign in with local passwords. TLS is recommended; the install guide covers a local certificate or a reverse proxy.
- **Institution tier.** A public address with HTTPS, as the Foundry runs today. Users sign in with ORCID. Managed or institutional Postgres and object storage may replace the bundled services.

Every address a browser or client must reach (the API, the Hub, presigned storage URLs) is configurable as a public URL separate from the internal compose address, because presigned URL signatures cover the host.

## Decisions

### Core install

- Services: Postgres (with the `vector` and `pg_trgm` extensions), the API, the Hub, and an S3-compatible object store. Message brokers belong to the plug-ins, not the core.
- The object store must be open source. MinIO removed features from its free edition and stopped community binary releases in 2025, so compare Garage, SeaweedFS, and MinIO before choosing (builder's call, recorded in the PR).
- The installer becomes platform administrator. There is no admin bootstrap today; one is needed.
- The Hub talks to the API by URL and stores sessions in plain Postgres or Redis. `HUB_SESSION_DATABASE_URL` and `HUB_SESSION_REDIS_URL` already exist in crucible-hub; its Cloud SQL connector is a Foundry option. Phase 1 can use the current Hub image.

### Sign-in

Two sign-in methods, chosen by setting; either or both can be enabled:

- **ORCID** (recommended for any deployment beyond a few users or on a public address). The Foundry keeps it. Registering an ORCID public API client is free, open to any ORCID account holder, and a short documented step. ORCID accepts only HTTPS redirect URIs in production and requires them to match exactly, so ORCID sign-in needs an HTTPS address for the API. Whether ORCID accepts a lab-network HTTPS hostname is unverified; the install guide must not promise it.
- **Local password login** (lab tier, a few users). Deliberately small, following NIST SP 800-63B revision 4 (August 2025) and the OWASP Password Storage and Authentication cheat sheets:
  - Accounts are created by a platform administrator with a temporary password that must be changed at first sign-in. No self-registration and no email reset, so no mail server is needed; an administrator resets forgotten passwords, which again forces a change at next sign-in. (Design call; self-registration with admin approval can be added later.)
  - Hashing: Argon2id through `pwdlib` (`pwdlib[argon2]`, the library FastAPI's documentation now uses; `passlib` is unmaintained and breaks on Python 3.13). Parameters at least OWASP's minimum, m=19 MiB, t=2, p=1, tuned so a hash takes well under a second on a workstation. Salts are generated by the library. Hashes are rehashed on sign-in when parameters are raised. No bcrypt (72-byte limit) and no custom schemes.
  - Pepper: an optional secret key from settings, applied as an extra keyed step (NIST "SHOULD"), stored outside the database. If set, it must be backed up with the deployment's secrets; losing it invalidates every password.
  - Password rules (NIST): passwords are the only factor, so the minimum is 15 characters; accept at least 64 characters, all printable ASCII, spaces, and Unicode (each code point counts as one character); no composition rules; no periodic expiry; reject passwords on a blocklist of common and breached passwords plus context words (the service name, the username). The blocklist ships with the image so it works offline; an online breached-password check (k-anonymity range queries) is optional.
  - Allow password managers and paste; no password hints or security questions.
  - Failed attempts: counted per account, not per IP, with exponential back-off between attempts and a hard lock after at most 100 consecutive failures (NIST ceiling) that only an administrator clears. A per-IP throttle may be added on top. Every failure, lock, and reset is logged.
  - The same generic error for unknown user, wrong password, and locked account, and the same work done in every case (a dummy hash verification for unknown users) so response time does not reveal which accounts exist.
  - Changing a password requires the current password, and revokes the user's other Hub sessions. API keys are separate credentials and are not affected.
  - The API serves a minimal sign-in form at the point where it otherwise redirects to ORCID, so the Hub's sign-in flow with the API (`/auth/hub/*`) is unchanged. nano-crucible and the uploaders keep using API keys.
  - Login must be served over HTTPS whenever it crosses a network; the install guide treats plain HTTP as acceptable only on a single machine.
- How a user signs in is stored as its own record (method plus external identifier, such as an ORCID iD or a local username), not encoded in the user identifier. Moving a group from passwords to ORCID later, or adding another provider, then adds a record instead of migrating identities.
- A user without an ORCID gets an MFID in the user identifier column, as service accounts already do. Separating identity from ORCID for everyone is the planned user-MFID work in `mfid_naming_transition.md`, not part of this project.
- A bundled identity provider (Keycloak, Zitadel, Authentik) or generic OIDC support is not part of this project; the sign-in record above leaves room for it.

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
- Before `crucible-api` is made public, the Foundry's own deployment configuration (`k8s-conf/`, `cloudbuild.yaml`, and anything naming Foundry hosts, addresses, project numbers, buckets, or the cluster) moves to a private repository, for example `crucible-foundry-deploy`. The public repository keeps only the software and generic ways to run it (the compose bundle, later perhaps a Kubernetes template).
- Where deployment files live (decided 2026-10-27): the API repository carries the generic, vendor-neutral self-hosting pieces any lab uses: the Docker Compose bundle in `deploy/compose/`, including the optional Hub overlay and the `/hub/` route, the install guide, and their CI. The Foundry's own deployment (GKE manifests, build and deploy steps, its settings and secret references) moves to the private repository `mf-crucible-deploy`. Revisit a separate self-hosting repository that only pins component images once the API and the Hub publish versioned images.
- The Foundry deploys by version (GitOps). The API repository builds and publishes an image for each version tag and deploys nothing on merge. The private deploy repository names the exact image version each environment runs; deploying staging, promoting to production, and rolling back are each a reviewed one-line change of that version. The deploy repository is applied either by a small CI job on change or by a controller in the cluster that reads it (Argo CD or Flux). Order: create the deploy repository from today's `k8s-conf/` and deploy steps; switch the cluster from `:latest` to explicit versions; remove `k8s-conf/` and the deploy steps from the API repository before it goes public. A change that needs new code and new config lands as two pull requests in order, config first when the code depends on it.
- Git history is published along with the code, and the private history contains Foundry infrastructure details and, at least once, committed secret files that were later deleted. Before publishing: scan the full history of every repository going public with a secret scanner (gitleaks or trufflehog) and rotate every real secret it finds, then publish from a fresh repository that starts from one clean snapshot of the current code, keeping the private repository as the archive. Rewriting the existing history (`git filter-repo`) is the fallback if keeping history matters, with the same scan and rotation.

- Release builds in a public repository (reviewed 2026-10-28, for when `crucible-api` goes public). `cloudbuild.release.yaml` itself holds nothing sensitive: the project comes from `${PROJECT_ID}`, the identity is set on the trigger, logs stay in Cloud Logging, and the tag name cannot inject shell (the trigger accepts only `v<digits>.<digits>.<digits>`). Outsiders cannot trigger it (tags only, never pull requests or forks), and the build account can only push to `crucible-api-releases` and write logs, with immutable tags. Before or at publication:
  - Restrict who can create, move, or delete `v*` tags with a GitHub tag ruleset (maintainers only). Today anyone with write access can tag an unreviewed commit and get a release image that staging would then follow; rulesets are not available on the current private plan, so turn them on when the repository becomes public.
  - Pin the base images by digest (`python:3.11-trixie` and `ghcr.io/astral-sh/uv` in the Dockerfile, `gcr.io/cloud-builders/docker` and `cloud-sdk:slim` in the build), with Dependabot proposing digest updates, so a release is reproducible from its commit.
  - Optional: let Cloud Build push the image through `images:` to record signed build provenance the deploy repository can check, and require an approval on the release trigger (`--require-approval`).

### Settings and secrets

- Settings are plain environment variables: from Kubernetes ConfigMaps kept in git for the Foundry, from a `.env` file for compose.
- Secrets never live in git. The application reads a secret from `<NAME>` or from the file named by `<NAME>_FILE` (the Docker secrets convention), and knows nothing about where it is stored.
- Transition for the Foundry: Phase 1 keeps the application's own Secret Manager lookup, driven by explicit `<NAME>_VERSION` references in the ConfigMaps, so nothing changes for the running deployment. A later, separate change moves the Foundry to secrets injected by the platform (GKE's Secret Manager add-on or External Secrets), verified on staging first; after that the Secret Manager code and `_VERSION` support are removed from the application. Secret references stay pinned to explicit versions rather than `latest` where practical.

## Phases

Each phase ships on its own and is tested before the next. Phases 1 to 4 are the core; Phase 5 is the plug-in stage.

1. **A fresh install works.**
   - A real baseline migration: today the Alembic baseline is empty and the schema came from archived SQL, so a new database cannot reach head. The baseline creates the extensions and the schema and only runs on an empty database; existing databases are untouched.
   - Explicit settings replace scattered defaults: Secret Manager project `776258882599`, RabbitMQ `10.128.0.31`, `mf-storage-prod` / `crucible-prod`, `mqtt.mfdata.org`, `foundry-admin.lbl.gov`, the branch-dependent port in the Dockerfile, and the root-HOME credential assumption. Missing secrets fail loudly instead of falling back to Secret Manager; Secret Manager becomes an explicit opt-in.
   - Optional features can be switched off: printing, the proposal integration, ingestion publishing, record archiving.
   - An admin bootstrap.
   - A `docker compose` setup with Postgres, the API, and the Hub (built from source until releases exist), plus a short install guide.
   - After the Phase 1 release, before Phase 2: **the Foundry deploy repository**. Tag images by version as well as commit; move `k8s-conf` and the deploy script to a private repository; staging follows new versions automatically and production changes by a reviewed version bump (the deploy tool is its own short design); then switch the Foundry to the GKE Secret Manager add-on and remove in-app Secret Manager fetching (`docs/self_hosting_secrets.md`). This moves the deployment part of Phase 4 forward; publishing images and public repositories stay in Phase 4.
2. **One S3-style storage layer** as described above, the object store added to compose, and the legacy upload routes deprecated.
3. **Local password login** for the lab tier, and the sign-in record that both methods use.
4. **Releases**: licenses, public repositories, published images (release images on GHCR for labs, the same digest in Artifact Registry for the Foundry), versioned compose bundle, upgrade path, the Foundry deployment configuration moved to a private repository, and the Foundry deploying by version from it. The license review can start immediately and run alongside Phases 1 to 3.
   - A named operator command: package the server (`[project.scripts]`, which also means renaming the generic top-level `app` package) so `python -m app.cli ...` becomes one command. Not `crucible`, which nano-crucible already ships as its client command. Proposed: `crucible-server` (`crucible-server db upgrade`, `crucible-server admin create`); alternatives in the logbook entry of 2026-10-26. Check the name is free on PyPI before deciding.
5. **Plug-ins**: ingestion and post-processing (RabbitMQ), then printing and telemetry (MQTT).
6. **Later:** HPC execution, Globus.

## Not breaking the Foundry deployment

1. Pin first, then change defaults. Every value the code assumes today goes into the Foundry config (`k8s-conf` ConfigMaps) and is deployed before the code default changes.
2. The baseline migration only runs on an empty database. A check builds a fresh database from migrations and compares its schema with a schema-only dump of staging; any difference fails.
3. Storage switches by setting, one environment at a time. The current GCS adapter stays until the S3-style path is proven. Staging switches first and runs the `crucible-tests` end-to-end suite against the real staging bucket through the S3-compatible API; production switches only after that, and switching back is a config change.
4. Sign-in: the Foundry keeps ORCID with local passwords disabled. Moving ORCID identities into the new sign-in record is a data migration tested on staging first; existing users keep their ORCID identifiers and sessions.
5. The HTTP contract changes only on purpose: the OpenAPI snapshot diff in each PR, deprecation headers and client coordination before removing routes, `crucible-tests` on staging before every merge to `main`, rolling staging-first deploys.
6. A CI job starts the compose setup on every PR and runs a smoke test (create a project, upload a file, download it), so Foundry changes cannot silently break self-hosting and the other way round.

## Clients

- crucible-hub: needs only its API URL and a session store; make the footer, QR trusted host, and Foundry branding configurable.
- nano-crucible: change the default URL and setup text, make Google libraries optional, and use presigned multipart only once the legacy routes are retired.
- crucible-upload-uis: read the API URL from config instead of hard-coding `https://crucible.lbl.gov/api/v3`, make printing optional, and ship a generic instrument template.
- Client changes go through the nano-crucible and crucible-hub sessions, not edits from this repo. Who changes the plug-in repositories is decided when Phase 5 starts.

## Left to the builder

- Choice of object store for compose and of the Python S3 client library.
- Exact back-off schedule, Argon2id parameters above the minimum, and which common-password list ships with the image.
- How settings are structured (one settings object, env var names), as long as they are documented and fail loudly when required values are missing.
- How the baseline migration is produced (squash, `create_all` plus stamp, or generated SQL), as long as the schema comparison passes.
- How the admin bootstrap works (first sign-in, a CLI command, or an env var naming the administrator).
- The order of pull requests inside a phase.
