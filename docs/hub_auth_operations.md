# Hub authentication operations

The Hub authentication routes use exact configured callbacks, OIDC validation, a one-use PKCE-bound handoff, and hashed API credentials. Code consumption and token issuance commit together. Refresh locks the family and reloads the token state after acquiring that lock; concurrent reuse revokes the family.

## Logging and secrets

Hub authentication responses, including validation errors, redirects, missing routes, and method errors under `/auth/hub`, carry `Cache-Control: no-store`, `Pragma: no-cache`, and `Referrer-Policy: no-referrer`. Validation failures retain HTTP 422 and the `detail` array shape but use a fixed message without submitted values or validation context. Unexpected authentication failures return a generic HTTP 500; application logs retain only the exception class, status, route, and request ID, not exception text or traceback. Other API routes retain their existing error handling. Proxy-generated errors require the corresponding ingress policy separately.

The application disables Uvicorn access logging because it includes OAuth callback query strings. Its request middleware continues to record method, route, status, duration, and request ID. Do not re-enable raw access logging for these routes. Configure proxy, load-balancer, and tracing logs to omit query strings, authorization headers, cookies, request bodies, and redirect Location headers on both API and Hub authentication routes. Scrub retained logs according to the existing retention policy.

Store the ORCID client secret and Hub client secret separately in the deployment secret manager. The Hub client secret is shared only with the Hub server. Local environment files should have mode `600`. Rotate any secret previously disclosed in chat; changing a local file does not revoke the old ORCID credential.

## Deployment credentials and activation

The API accesses Google Secret Manager directly with the existing Google client library and Application Default Credentials, using the pod's workload identity. It needs no exported service-account key or new CSI/External Secrets dependency. Secret values stay in worker memory, not ConfigMaps, Kubernetes Secret copies, logs, or files. The existing legacy API credential loader is unchanged. This follows [Google's Secret Manager guidance](https://docs.cloud.google.com/secret-manager/docs/best-practices).

Set `HUB_AUTH_CLIENT_SECRET_VERSION` and `HUB_AUTH_ORCID_CLIENT_SECRET_VERSION` to full `projects/.../secrets/.../versions/N` names with numeric versions. A reference takes precedence over its corresponding inline environment secret; access failure never falls back to inline or legacy credentials. Successful reads are cached per worker and version; failed reads are not cached. Lookups have a five-second RPC timeout without SDK retries. Values must be nonempty ASCII without line breaks. Local development may keep `HUB_AUTH_CLIENT_SECRET` and `HUB_AUTH_ORCID_CLIENT_SECRET` when no references are configured.

The following secrets were provisioned under project `776258882599` on 2026-09-28. Main's Hub client credential remains pinned to version `1`; staging's Hub client credential and both ORCID credentials were rotated to version `2`. Their values were verified without logging them. Existing unrelated secrets and IAM grants were not changed.

After successful local sign-in and logout testing, superseded version `1` of the staging Hub client secret and both ORCID secrets was disabled with user approval. Main's shared Hub credential remains enabled at version `1`. Disabled versions were not destroyed; do not re-enable exposed credentials for rollback.

| Environment | Hub client credential | ORCID client credential |
| --- | --- | --- |
| Main | `crucible-hub-main-client-secret` | `crucible-hub-main-orcid-client-secret` |
| Staging | `crucible-hub-staging-client-secret` | `crucible-hub-staging-orcid-client-secret` |

Main has a separately generated Hub client secret; staging matches the rotated local Hub/API shared credential. The API and corresponding Hub server must use the same value for their environment. Both ORCID secrets contain the rotated local credential for configured client `APP-YDW1MRE405389PP0`. The Hub never needs the ORCID client secret. Only main gets a deployed Hub. Staging is tested from a local Hub with an explicitly configured callback.

The user accepted the existing `mf-crucible` project trust boundary for this release. Existing project-wide Secret Accessor principals inherit access to the new secrets. Main and staging also share the `default` Kubernetes identity, which now has secret-level access on these four new secrets. This is not per-environment workload isolation. No existing principals were migrated and no grants were removed. Any future IAM isolation is a separate audited change. A dedicated Hub runtime grant must be checked when its Cloud Run identity is selected.

Both API ConfigMaps ship with `HUB_AUTH_ENABLED=false`. This disables new login and client-authenticated exchange/refresh/revoke operations without fetching secrets. Callback attempts return the generic sign-in failure. It does not invalidate existing access tokens or legacy API keys and is not an account-suspension switch. If the flag is absent, existing local configuration remains compatible. Secret unavailability returns a generic 503 at login and client-authenticated endpoints, or the generic callback failure.

Production reserves `https://crucible.lbl.gov/hub/auth/callback` as the Hub callback and `https://crucible.lbl.gov/api/v3` as the public API URL. Register `https://crucible.lbl.gov/api/v3/auth/hub/callback` with ORCID. Staging uses `https://crucible.lbl.gov/testapi-staging`, whose ORCID callback is `https://crucible.lbl.gov/testapi-staging/auth/hub/callback`. Its Hub callback remains blank until a local Hub is chosen, for example the previously tested HTTPS Tailscale callback. Set exactly the same callback in the API and local Hub; no wildcard or separate staging Hub deployment is required.

Before activation:

Follow the proposed [schema rollout sequence](hub_auth_release_rollout.md) for the exact-readiness transition. Its recorded staging mismatch must be resolved before merging the release.

1. The four secrets and API secret-level grants are provisioned. When deploying the Hub, confirm its runtime identity can access only the main shared client secret unless it already inherits broader project access. Do not add new project-wide grants.
2. Confirm the pinned numeric references remain enabled. Do not put secret values in shell arguments, git, or chat. Future rotations require a separate approved version update; do not recreate or overwrite the provisioned credentials.
3. Coordinate the approved schema migration and deployment. `EXPECTED_DB_REVISIONS` is `4a7d2c9e1f60` for main and `d81c3a4f62b0` for staging; update it to the verified target `e92a6b1c730f` as part of that coordinated rollout, not as an unrelated configuration push. The readiness check uses an exact revision match, so migrating while old pods still expect the previous revision can make them unready. Plan the transition explicitly before running Alembic.
4. Verify ingress protections and backend bypass policy, configure the exact callback, and enable `HUB_AUTH_ENABLED=true` only for the environment under test. Roll the API pods to load the ConfigMap changes. The Hub's production activation remains independently controlled.
5. Test sign-in, refresh, logout, secret unavailability, and non-Hub clients. Run the cleanup preview and approved first cleanup before enabling its schedule.

Rotation adds a new secret version, updates the pinned reference, and rolls the consuming processes. Coordinate shared-client-secret rotation with the Hub; only one shared value is accepted at a time, so mixed old/new replicas need a planned transition. Cached values remain in existing workers even if the Secret Manager version is later disabled. Emergency credential revocation therefore also requires process rotation and, where appropriate, separate token-family revocation. Do not confuse Secret Manager version disablement with immediate session logout.

## Proxy throttling

The implementation and rollout guide now live in the `crucible-nginx` repository: `nginx.conf`, `docs/hub-auth-ingress.md`, and `tests/check_hub_ingress.py`, on branch `feat/hub-auth-ingress`. They cover main and staging Hub authentication paths, separate browser/server rate limits, 8 KiB request bodies, proxy timeouts, and sanitized access logs. Normal upload limits are unchanged. This replaces the earlier illustrative configuration; do not deploy both.

The feature was tested locally against Nginx 1.25.3 with mock upstreams, but has not been deployed. Tailscale Serve does not inherit these controls. Main's public API load balancer remains directly reachable, so proxy-only limits are incomplete until backend access is addressed without breaking existing clients. Cloudflare IP trust, logging before location selection, and the actual source addresses must be checked during the approved staged rollout. No firewall or Kubernetes Service restrictions have been changed.

## Expired-record cleanup

Staging and main CronJobs and their approval-gated activation are documented in [Kubernetes deployment notes](../k8s-conf/README.md#hub-authentication-cleanup). Cloud Build deploys them with the API image SHA, initially suspended, and preserves their activation state on subsequent releases.

Run `scripts/cleanup_hub_auth.py` from a scheduled maintenance job using the API deployment credentials. It defaults to a read-only preview and emits counts only. It deletes expired login transactions, handoff codes, and access tokens in bounded batches. It deletes refresh records and families only after absolute family expiry, including revoked families, retaining spent-token history for replay detection. It never revokes a live session.

```bash
DB_CONF_FILE=.env .venv/bin/python scripts/cleanup_hub_auth.py
DB_CONF_FILE=.env .venv/bin/python scripts/cleanup_hub_auth.py --commit --batch-size 1000 --batches 10
```

Schedule the commit command hourly, prevent overlapping jobs, and monitor counts and execution time. No staging or production cleanup has been scheduled or run as part of this change. Revision `e92a6b1c730f` adds composite `(expires_at, primary_key)` indexes matching cleanup filtering and batch ordering on login transactions, handoff codes, access tokens, and token families. Refresh records use their existing family index. Batch limits bound selected rows; removing one expired family also removes all of its remaining credentials.

The expiry migration creates and drops indexes concurrently so ordinary reads and writes can continue. It adds no tables, changes no token lifetimes, and deletes no records. Concurrent builds run outside a transaction and may wait for existing transactions; a failed migration can leave completed or invalid indexes. Before retrying, inspect the four `ix_hub*_expiry` indexes and remove only those created by this revision with `DROP INDEX CONCURRENTLY`, then rerun the revision. Do not stamp the revision or skip an existing index without verifying its definition and validity. Applying this revision to staging or production requires deployment approval.

## Verification and remaining deployment gates

PostgreSQL regression tests require an isolated local database named `crucible_hub_auth_test`. They create and drop a uniquely named schema and reject non-loopback hosts or other database names. Never point them at a staging or production proxy.

```bash
HUB_AUTH_TEST_DATABASE_URL=postgresql://localhost/crucible_hub_auth_test \
  .venv/bin/pytest -q tests/test_hub_auth_postgres.py tests/test_hub_tokens.py
```

The limited production pilot uses `HUB_AUTH_SESSION_MODE=single-instance`, process-local Hub sessions, one minimum and one maximum Cloud Run instance, and best-effort session affinity, with no traffic splitting. Restart, replacement, or misrouting can require a fresh sign-in. Redis and a Hub-owned session database are not pilot prerequisites. Shared-session continuity and multi-replica operation are deferred; the Hub's decision 0033 selects a future Hub-owned Cloud SQL store before scaling.

Before pilot activation, verify CSRF defenses, browser refresh, concurrent refresh within the Hub process, logout, two-user isolation, revocation, and restart sign-out through the deployed proxy. API token tables and their cleanup remain required independently of where Hub browser sessions live. Hub tokens use current API permissions on every request and do not use the legacy API-key cache.

Account suspension remains a separate schema and policy change: the API currently has no disabled-account field. Revoking a Hub family ends that session but does not prevent a new ORCID login or revoke legacy keys. A future account suspension must reject new login, handoff exchange, refresh, and all authenticated requests, including legacy keys, and define cache invalidation before it is enabled. Do not present session revocation as account disablement.
