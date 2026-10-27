# The Hub in a self-hosted installation: plan

Status: agreed 2026-10-27. Replaces the short Task 9 entry in `self_hosting_phase1_plan.md`.

## Goal

A lab that runs the self-hosted API (Task 7 bundle) can add the Crucible Hub, the browser interface, by configuration only, on the same workstation or on a second machine, under the same public address. The API never depends on the Hub; the Foundry deployment does not change.

## Decisions (2026-10-27)

1. The Hub is a separate component wherever it runs: its own Compose project, its own image and version, its own settings and secrets, and its own PostgreSQL for sessions. It talks to the API only through the API's HTTP interface.
2. One front door (Caddy in the API bundle) serves both: `/api/v3/` to the API with the prefix stripped, `/hub/` to the Hub with the prefix kept.
3. Placement is configuration: on one workstation the Hub project joins the API project's network; on two machines the front door forwards `/hub/` over a private network or HTTPS and the Hub reaches the API by its public address. Both are in Phase 1 (Task 9).
4. Sessions use the Hub's `postgres` mode only. The user would like the Hub's `single-instance` mode retired; that is crucible-hub's change to make.
5. Until the Hub publishes versioned images, the Hub is built from its repository at a pinned release tag.
6. Phase 2 makes the bundled database and storage optional, so a lab can use its institution's PostgreSQL or S3 by configuration.

Why this shape:

- Separate means separate boundaries, not separate machines: its own deployable unit, its own data, and access to others only through their interfaces. Database-per-service guidance allows a shared server with separate databases, but a separate small instance makes adding, upgrading, and removing the Hub independent of the API's data (`down -v` in the Hub project leaves nothing behind); its cost is one small PostgreSQL container (tens of megabytes at idle).
- One gateway in front of separately deployable, optional services is how comparable self-hosted products are packaged (Supabase's Docker setup puts its dashboard and every API behind one gateway on one address and advises never exposing individual service ports).
- The Hub is a Backend for Frontend: the browser holds only an opaque session cookie and the Hub holds the tokens, which the IETF best current practice for browser-based OAuth apps describes as the most secure of its patterns.
- Compose is single-machine; a second machine is a second Compose project, with the front door forwarding to it. Orchestrators are out of scope.
- Bundling the API, its database, and storage in one Compose project is right for a small lab, provided each backing service is replaceable by configuration (twelve-factor "backing services as attached resources"; GitLab bundles PostgreSQL, Redis, and object storage in its package and runs the same software against managed services, recommended for production at scale).

## Facts from crucible-hub (2026-10-27, v1.6.7)

- Node 22 server (TanStack Start), one container on port 3000, served only under `/hub` (fixed at build time), health at `/hub/health`.
- Sign-in in production is ORCID through the API: browser to `/hub/auth/login`, to the API's `/auth/hub/login`, to ORCID, back to the API's `/auth/hub/callback`, then to the Hub's `/hub/auth/callback`; the Hub exchanges a one-time code with the API server to server. No production sign-in without ORCID until the API adds local passwords (Phase 3); the Hub then needs only wording changes.
- The lab registers one redirect with ORCID: the API's `https://<site>/api/v3/auth/hub/callback`. The API allows the Hub's callback through `HUB_AUTH_CALLBACK_URI`. API and Hub share `HUB_AUTH_CLIENT_SECRET`.
- The browser side must be HTTPS. The Hub listens on plain HTTP behind the proxy.
- `postgres` session mode stores encrypted entries (key ring `HUB_SESSION_KEYS`) in schema `hub`, created by an owner role; the Hub's migration script, run as that owner, creates the table and grants an app role select, insert, update, delete on it only.
- No host name is hard-coded: `HUB_AUTH_CALLBACK_URI` gives the Hub's public address, `CRUCIBLE_API_BASE_URL` the server-to-server API address, `HUB_AUTH_PUBLIC_API_URL` the browser-facing one.
- The Hub reads the client address from `X-Forwarded-For` for its sign-in rate limit, so it must be reachable only through the front door.
- Uploads (Phase 2) go from the browser to object storage with signed links: the store must be reachable by browsers and allow the Hub's origin (CORS).

## Hub changes (crucible-hub repository and session)

| | Change | Why |
|---|---|---|
| H1 | `postgres` session mode accepts a plain `HUB_SESSION_DATABASE_URL` in production (TLS through `sslmode`) | today production requires a Cloud SQL instance name |
| H2 | secrets read as `NAME_FILE`, then `/run/secrets/NAME`, then `NAME` | Compose secrets; the API's contract |
| H3 | explicit opt-in for a plain-HTTP `CRUCIBLE_API_BASE_URL` (never the public URL) | `http://api:8000` on one workstation; tokens cross that hop, so opt-in only |
| H4 | session database bootstrap and migrations in the image, with entry points for a one-shot setup service | the Hub project creates its own database, schema, and roles; the running Hub keeps app-role credentials only |
| H5 | trusted-proxy setting for `X-Forwarded-For` | two-machine placement, or any Hub reachable other than through the front door |
| H6 | `/hub/health` includes the session database | a meaningful health check for Compose and the proxy |
| H7 | the Hub's own Compose project (Hub, its PostgreSQL, the setup service), versioned with the Hub, documented for both placements | recommended to live in the Hub repository, since it is the Hub's deployment; the API guide links to it |
| H8 (Phase 4) | versioned public images (multi-arch) with `org.opencontainers.image.version` and `revision`, and a minimum-API-version label in reverse-domain form | pinning by version or digest; compatibility checks |
| H9 (crucible-hub's call) | retire `single-instance` session mode | one session path; restarts keep people signed in |

## Hub release 1.7.0 (crucible-hub, in progress since 2026-10-27)

H1 to H7 were approved by the user in the crucible-hub session; the bundle pins `v1.7.0` once tagged. Names:

- H3: `HUB_API_ALLOW_HTTP=true` permits an `http://` `CRUCIBLE_API_BASE_URL`; `HUB_AUTH_PUBLIC_API_URL` and the callback stay HTTPS-only.
- H5: `HUB_TRUSTED_PROXY_HOPS=N`, the client address is the Nth `X-Forwarded-For` entry from the right; `0` uses the socket address; default `1`, which matches Caddy (it replaces the header). An institutional proxy in front of Caddy means 2, and Caddy's `trusted_proxies` must then name that proxy.
- H9: `single-instance` is deprecated in 1.7.0 and removed in the next major release; the bundle uses `postgres` only.
- H4: a one-shot setup entry point creates the owner and app roles and schema `hub`, then migrates, from an admin URL; the running Hub uses the app role only. Exact command and settings in the Hub's `deploy/compose` README.
- H6: `/hub/health` answers 503 when the session database is unreachable.
- H7: the Hub's Compose project is in the Hub repository (`deploy/compose`), joining an external network (default `crucible`) for the same workstation, with a standalone variant for a second machine.
- `HUB_AUTH_PUBLIC_API_URL` is needed in both placements: the browser follows it for sign-in and cannot resolve `api:8000`.

## API repository changes (Task 9)

1. Caddy (`deploy/compose/Caddyfile`): `handle /hub/*` (no prefix strip) to `{$CRUCIBLE_HUB_UPSTREAM}`, empty by default; `redir /hub /hub/`; when no Hub is configured, `/hub/` answers 404 "The Hub is not installed on this server". For `/api/v3/auth/hub/*`: a request body limit and shorter timeouts, as the Foundry's nginx sets. Caddy ignores incoming `X-Forwarded-For` by default and sets its own.
2. The API project's network gets a fixed name (for example `crucible`), so the Hub project on the same workstation can join it as an external network and be reached as `hub:3000`, reaching the API as `api:8000`.
3. API settings for Hub sign-in, in `.env` and secret files: `HUB_AUTH_ENABLED` (default `false`), `HUB_AUTH_CALLBACK_URI`, `HUB_AUTH_PUBLIC_API_URL`, `ORCID_CLIENT_ID`, `HUB_AUTH_ORCID_ISSUER` (`https://orcid.org`, or `https://sandbox.orcid.org` for a trial); secrets `HUB_AUTH_CLIENT_SECRET` and `ORCID_CLIENT_SECRET`. An empty secret file counts as not given, so installations without the Hub or ORCID are unchanged. The API already requires HTTPS for these addresses unless the host is `localhost`.
4. Install guide, "Adding the Hub":
   - HTTPS is required: a public domain name (automatic certificates; ports 80 and 443 reachable); a domain whose DNS the lab controls but that the internet cannot reach (DNS challenge, needs a Caddy build with the DNS provider's module; documented, not shipped); or the institution's certificate (`tls cert.pem key.pem`). Caddy's local certificate authority (`tls internal`) is for evaluation, because every user's machine must trust it.
   - ORCID: Public API credentials from ORCID's developer tools (free; verified email), redirect `https://<site>/api/v3/auth/hub/callback`; production accepts HTTPS redirects only and matches the domain exactly; the sandbox for trials.
   - Same workstation: start the API bundle, then the Hub project (it joins the `crucible` network), set `CRUCIBLE_HUB_UPSTREAM=hub:3000`.
   - Second machine: the Hub project with its public API address; the Hub host's port open only to the front door (firewall rule or private network such as WireGuard or Tailscale); `CRUCIBLE_HUB_UPSTREAM` set to that host, over HTTPS (`reverse_proxy https://...` with `tls_server_name`) unless the network is private.
5. Tests:
   - CI: the API bundle plus the Hub project built at the pinned tag, site at `https://localhost` with Caddy's internal CA: `/hub/health` through the proxy, the Hub's setup service ran, and the Hub cannot reach the API's database. Both placements, the second simulated with two separate Docker networks. ORCID sign-in cannot run in CI.
   - Manual, by the user, once: ORCID sandbox sign-in end to end on a machine with a real HTTPS name.
   - Adding the Hub to an existing installation and removing it again leaves the API's data unchanged.

## Phase 2 additions

- Bundled database and storage optional: a profile or documented override removes `db` (and the storage service) when a lab points the API at its own PostgreSQL (with `vector` and `pg_trgm` installed by an administrator) or S3-compatible store.
- Storage reachable by browsers, with CORS for the Hub's origin.

## Follow-ups, not in Phase 1

- Rate limiting of sign-in routes: the Foundry's nginx limits `/auth/hub/*`; Caddy has no built-in limiter (a rate-limit module needs a custom build), or the API could limit them. The Hub already limits its own login.
- Hub horizontal scaling: verify concurrent token refresh across two Hub instances first (crucible-hub's caveat).
- The Foundry: whether the Hub moves from Cloud Run into the API's cluster is decided in the Foundry deploy repository step after the Phase 1 release.

## Order

1. Hub H1 to H7 (and H9 if crucible-hub agrees), released as a Hub version.
2. Task 9 here against that version; PR to staging (nothing in `deploy/compose` runs on the Foundry).
3. Phase 1 close-out (Task 10), then the single production release.

## Sources

- Docker, Compose profiles: https://docs.docker.com/compose/how-tos/profiles/
- Docker, init containers and `pre_start`: https://docs.docker.com/compose/how-tos/init-containers/
- Caddy, `handle_path`: https://caddyserver.com/docs/caddyfile/directives/handle_path
- Caddy, `reverse_proxy`: https://caddyserver.com/docs/caddyfile/directives/reverse_proxy
- Caddy, automatic HTTPS: https://caddyserver.com/docs/automatic-https
- Caddy, `tls` directive: https://caddyserver.com/docs/caddyfile/directives/tls
- Caddy community, DNS provider modules: https://caddy.community/t/how-to-use-dns-provider-modules-in-caddy-2/8148
- ORCID, registering a Public API client: https://info.orcid.org/documentation/integration-guide/registering-a-public-api-client/
- ORCID, integration and API FAQ: https://info.orcid.org/documentation/integration-and-api-faq/
- IETF, OAuth 2.0 for Browser-Based Applications: https://datatracker.ietf.org/doc/draft-ietf-oauth-browser-based-apps/
- MDN, X-Forwarded-For: https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/X-Forwarded-For
- Supabase, self-hosting with Docker: https://supabase.com/docs/guides/self-hosting/docker
- The Twelve-Factor App, backing services: https://12factor.net/backing-services
- GitLab, cloud services for GitLab components: https://docs.gitlab.com/install/cloud-services/
- GitLab, external PostgreSQL: https://docs.gitlab.com/administration/postgresql/external
- microservices.io, database per service: https://microservices.io/patterns/data/database-per-service.html
- OCI image spec, annotations: https://github.com/opencontainers/image-spec/blob/main/annotations.md
- PostgreSQL, schemas: https://www.postgresql.org/docs/current/ddl-schemas.html
