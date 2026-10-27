# The Hub in a self-hosted installation: plan

Status: proposed 2026-10-27, for approval. Replaces the short Task 9 entry in `self_hosting_phase1_plan.md`.

## Goal

A lab that runs the self-hosted API (Task 7 bundle) can add the Crucible Hub, the browser interface, by configuration only, either on the same machine or on a second one, under the same public address. The API never depends on the Hub; the Foundry deployment does not change.

## Decision

Separate containers behind one front door (Caddy): `/api/v3/` goes to the API with the prefix stripped, `/hub/` goes to the Hub with the prefix kept. The Hub is an optional unit: a Compose profile on the same machine, or its own Compose project on a second machine that the front door forwards to.

This matches common practice:

- One gateway in front of separately deployable services, each optional, is how comparable self-hosted products are packaged; Supabase's Docker setup puts its dashboard and every API behind one gateway (Envoy by default, Kong optional) on one address, and advises never exposing individual service ports.
- Compose profiles are Docker's documented way to keep optional services out of the default `up`; core services stay unprofiled.
- The Hub is a Backend for Frontend (BFF): the browser holds only an opaque session cookie and the Hub holds the tokens. The IETF best current practice for browser-based OAuth apps describes the BFF as the most secure of its patterns. A BFF is deployed next to its users' front door and scales on its own.
- Compose is a single-machine tool; two machines means two Compose projects with the front door forwarding to the second over a private network or TLS. Beyond a few machines the standard step is an orchestrator (Kubernetes, Nomad), which is out of scope.

## Facts from crucible-hub (2026-10-27, v1.6.7)

- Node 22 server (TanStack Start), one container on port 3000, served only under `/hub` (fixed at build time), health at `/hub/health`.
- Sign-in in production is ORCID through the API: browser to `/hub/auth/login`, to the API's `/auth/hub/login`, to ORCID, back to the API's `/auth/hub/callback`, then to the Hub's `/hub/auth/callback`; the Hub then exchanges a one-time code with the API server to server. There is no production sign-in without ORCID until the API adds local passwords (Phase 3); the Hub then needs only wording changes.
- The lab registers one redirect with ORCID: the API's `https://<site>/api/v3/auth/hub/callback`. The Hub's callback is allowed by the API's `HUB_AUTH_CALLBACK_URI`. API and Hub share `HUB_AUTH_CLIENT_SECRET`.
- The browser side must be HTTPS (secure cookies, callbacks). The Hub's own listener is plain HTTP behind the proxy.
- Sessions: `single-instance` (in memory, signed out on restart, exactly one instance) or `postgres` (encrypted entries, needs a session key ring `HUB_SESSION_KEYS`). The schema `hub` is created by the database owner; the Hub's migration script, run as an owner role, creates the table and grants an app role select, insert, update, delete on it only.
- No host name is hard-coded. `HUB_AUTH_CALLBACK_URI` gives its public address; `CRUCIBLE_API_BASE_URL` is the server-to-server API address; `HUB_AUTH_PUBLIC_API_URL` the browser-facing one (defaults to the former).
- The Hub reads the client address from `X-Forwarded-For` for its sign-in rate limit, so it must be reachable only through the front door.
- Uploads (Phase 2) go from the browser straight to object storage with signed links, so the store must be reachable by browsers and allow the Hub's origin (CORS).

## Hub changes (crucible-hub repository, its session, needs the user's go-ahead there)

| | Change | Why |
|---|---|---|
| H1 | `postgres` session mode accepts a plain `HUB_SESSION_DATABASE_URL` in production (TLS through `sslmode`) | today production requires a Cloud SQL instance name |
| H2 | secrets read as `NAME_FILE`, then `/run/secrets/NAME`, then `NAME`, in one server-only reader | Compose secrets; same contract as the API |
| H3 | an explicit setting that allows a plain-HTTP `CRUCIBLE_API_BASE_URL` (never the public URL) | `http://api:8000` inside the Compose network; tokens cross that hop, so it must be opt-in |
| H4 | session migrations and their runner in the image, with a migrate entry point | a one-time `hub-migrate` service, so the running Hub keeps app-role credentials only |
| H5 | a trusted-proxy setting for `X-Forwarded-For` | topology B and any deployment where the Hub could be reached directly |
| H6 | `/hub/health` reports the session database in `postgres` mode | a meaningful health check for Compose and Caddy |
| H7 (Phase 4) | versioned public images (GHCR, multi-arch) with `org.opencontainers.image.version` and `revision`, plus a minimum-API-version label in reverse-domain form | pinning by version or digest; checking compatibility |

H1 to H4 block this plan; H5 and H6 are small and should come with them; H7 joins the Phase 4 release work.

## API repository changes (this plan, Task 9)

### 9a. Hub on the same machine (topology A)

1. Compose services under `profiles: ["hub"]`:
   - `hub-db-setup`: one-shot, PostgreSQL client with the superuser secret. Idempotent SQL: create database `crucible_hub`, roles `crucible_hub_owner` (owns schema `hub`) and `crucible_hub_app`, `REVOKE ALL ON DATABASE crucible_hub FROM PUBLIC`, `REVOKE ALL ON DATABASE crucible FROM PUBLIC` with explicit `CONNECT` grants, so neither the API nor the Hub can connect to the other's database. A one-shot service rather than the `db-init` script, because `db-init` runs only when the volume is first created, and a lab may add the Hub to an existing installation.
   - `hub-migrate`: one-shot, the Hub image's migrate entry point (H4) as `crucible_hub_owner`; waits for `hub-db-setup`.
   - `hub`: the Hub as `crucible_hub_app`, `HUB_DATA_SOURCE=hub-auth`, `HUB_AUTH_PRODUCTION_ENABLED=true`, `HUB_AUTH_SESSION_MODE=postgres`, `CRUCIBLE_API_BASE_URL=http://api:8000` with the H3 opt-in, `HUB_AUTH_PUBLIC_API_URL=https://<site>/api/v3`, `HUB_AUTH_CALLBACK_URI=https://<site>/hub/auth/callback`; waits for `hub-migrate` and a healthy `api`; health check on `/hub/health`.
   The one-shot services use the long-standing `depends_on: condition: service_completed_successfully` pattern, which works with the Compose version the guide requires; Compose's newer `pre_start` hook is not used because the setup is shared and must be addressable on its own.
2. Secrets (files, the Task 6b contract): `HUB_AUTH_CLIENT_SECRET` (shared by API and Hub), `HUB_SESSION_KEYS`, `HUB_DB_OWNER_PASSWORD`, `HUB_DB_APP_PASSWORD`, `ORCID_CLIENT_SECRET`. The API service references the Hub client secret and the ORCID secret always; an empty file counts as not given, so an installation without the Hub or ORCID keeps empty files and nothing changes.
3. API settings in `.env`, passed to `api`: `HUB_AUTH_ENABLED` (default `false`), `HUB_AUTH_CALLBACK_URI`, `HUB_AUTH_PUBLIC_API_URL`, `ORCID_CLIENT_ID`, `HUB_AUTH_ORCID_ISSUER` (`https://orcid.org`, or `https://sandbox.orcid.org` for testing). The API already enforces HTTPS for these unless the host is `localhost` (`_trusted_public_url` in `app/utils/hub_login.py`).
4. Caddy: `handle /hub/*` (no prefix strip; `handle_path` strips) to `{$CRUCIBLE_HUB_UPSTREAM}` (default `hub:3000`), `redir /hub /hub/`, and a friendly 502 page ("the Hub is not installed on this server") when the Hub is absent. For `/api/v3/auth/hub/*`, a request body limit (`request_body max_size`) and shorter timeouts, as the Foundry's nginx sets. Caddy by default ignores incoming `X-Forwarded-For` and sets its own, so the Hub and API see the real client.
5. Version pairing: `.env` pins `CRUCIBLE_HUB_VERSION` to a Hub release tested with this API. Until H7 the image is built from the Hub repository at that tag (`build: <hub git URL>#<tag>`), which requires the repository to be readable; after H7 it is `image: ghcr.io/...:<version>@sha256:<digest>`.
6. Install guide, a "Hub" section:
   - HTTPS is required for the Hub. Three ways, depending on the network: a public domain name (Caddy gets a Let's Encrypt certificate automatically; ports 80 and 443 reachable); a domain whose DNS the lab controls but that is not reachable from the internet (DNS challenge, which needs a Caddy build with the DNS provider's module; we document it rather than ship one); or the institution's own certificate (`tls cert.pem key.pem`). Caddy's local certificate authority (`tls internal`) is for evaluation only, because every user's machine must trust its root certificate.
   - ORCID: register Public API credentials in ORCID's developer tools (free; email must be verified), with the redirect `https://<site>/api/v3/auth/hub/callback`. ORCID accepts only HTTPS redirects in production and matches the domain exactly. Use the ORCID sandbox for a trial.
   - Enable: `COMPOSE_PROFILES=hub` in `.env` (or `--profile hub`), fill the settings and secrets, `docker compose up -d --wait`.
7. Tests:
   - CI: a `compose-hub` job (or a step in `compose-smoke`) with `--profile hub`, the site at `https://localhost` with Caddy's internal CA, checks `/hub/health` through the proxy, that `hub-migrate` ran, and that the Hub cannot reach the `crucible` database. It cannot exercise ORCID.
   - Manual, by the user, once: ORCID sandbox sign-in end to end on a machine with a real HTTPS name.
   - Adding the Hub to an existing installation (`hub-db-setup` on a database that already holds data) and removing it again.

### 9b. Hub on a second machine (topology B)

1. `deploy/compose/hub/`: a Compose project for the Hub host with `hub-migrate`, `hub`, and its own small PostgreSQL for sessions (the same `hub-db-setup` SQL). The Hub reaches the API by its public HTTPS address, so no plain-HTTP opt-in is needed.
2. The front door on the API host forwards `/hub/*` to the Hub host: `CRUCIBLE_HUB_UPSTREAM` set to the Hub host over a private network (WireGuard, Tailscale, or the institution's network) or HTTPS (`reverse_proxy https://...` with `tls_server_name`). The Hub host publishes its port only to that network, and the Hub trusts forwarding headers only from the front door (H5).
3. Guide section with the steps and the firewall rule. Test: both projects on one machine with separate networks, the second reached only through the first.

### 9c. Follow-ups recorded, not in Phase 1

- Rate limiting of sign-in routes: the Foundry's nginx limits `/auth/hub/*`; Caddy has no built-in rate limiter. Options: Caddy's rate-limit module in a custom build, or limits in the API. The Hub already rate-limits its own login.
- Phase 2 storage: browser-reachable endpoint, CORS for the Hub's origin, and whether it is served under the same front door.
- Hub horizontal scaling: verify concurrent token refresh across two Hub instances before claiming it (crucible-hub's caveat).
- The Foundry: whether the Hub moves from Cloud Run into the same cluster as the API is decided in the Foundry deploy repository step after the Phase 1 release.

## Order

1. Hub H1 to H6 (crucible-hub session), released as a Hub version.
2. 9a here, against that version; PR to staging (the Foundry is unaffected: nothing in `deploy/compose` runs there).
3. 9b.
4. Phase 1 close-out (Task 10), then the single production release.

## Decisions for the user

1. Session mode in the bundle: `postgres` (recommended; upgrades keep people signed in) or `single-instance` (no database, simplest, signs everyone out on restart).
2. Hub sessions in a separate database `crucible_hub` (recommended) or a schema in the API's database.
3. Topology B in Phase 1 (9b) or after the Phase 1 release.
4. Whether the Hub repository will be readable for building from source until GHCR images exist (H7), or whether H7 moves earlier.

## Sources

- Docker, Compose profiles: https://docs.docker.com/compose/how-tos/profiles/
- Docker, init containers and `pre_start`: https://docs.docker.com/compose/how-tos/init-containers/
- Caddy, `handle_path`: https://caddyserver.com/docs/caddyfile/directives/handle_path
- Caddy, `reverse_proxy` (forwarded headers, trusted proxies, HTTPS upstreams): https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- Caddy, automatic HTTPS (public names, DNS challenge, local CA): https://caddyserver.com/docs/automatic-https
- Caddy, `tls` directive: https://caddyserver.com/docs/caddyfile/directives/tls
- Caddy community, DNS provider modules: https://caddy.community/t/how-to-use-dns-provider-modules-in-caddy-2/8148
- ORCID, registering a Public API client: https://info.orcid.org/documentation/integration-guide/registering-a-public-api-client/
- ORCID, integration and API FAQ (HTTPS redirect URIs in production): https://info.orcid.org/documentation/integration-and-api-faq/
- IETF, OAuth 2.0 for Browser-Based Applications (BFF): https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
- MDN, X-Forwarded-For (trusted proxies): https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Forwarded-For
- Supabase, self-hosting with Docker (one gateway in front of services): https://supabase.com/docs/guides/self-hosting/docker
- OCI image spec, annotations: https://github.com/opencontainers/image-spec/blob/main/annotations.md
- PostgreSQL, schemas and the public schema: https://www.postgresql.org/docs/current/ddl-schemas.html
- AWS, managing PostgreSQL users and roles (least privilege): https://aws.amazon.com/blogs/database/managing-postgresql-users-and-roles/
