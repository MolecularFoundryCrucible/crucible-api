# Hub authentication operations

The Hub authentication routes use exact configured callbacks, OIDC validation, a one-use PKCE-bound handoff, and hashed API credentials. Code consumption and token issuance commit together. Refresh locks the family and reloads the token state after acquiring that lock; concurrent reuse revokes the family.

## Logging and secrets

The application disables Uvicorn access logging because it includes OAuth callback query strings. Its request middleware continues to record method, route, status, duration, and request ID. Do not re-enable raw access logging for these routes. Configure proxy, load-balancer, and tracing logs to omit query strings, authorization headers, cookies, request bodies, and redirect Location headers on both API and Hub authentication routes. Scrub retained logs according to the existing retention policy.

Store the ORCID client secret and Hub client secret separately in the deployment secret manager. The Hub client secret is shared only with the Hub server. Local environment files should have mode `600`. Rotate any secret previously disclosed in chat; changing a local file does not revoke the old ORCID credential.

## Proxy throttling

Apply throttling at the trusted ingress before enabling public Hub sign-in. These settings are a deployment template, not active protection in the API process or Tailscale Serve. Use the existing proxy routing and upstream addresses. Prevent direct public access to the API backend from bypassing the ingress.

Add these directives to the Nginx `http` context:

```nginx
map $uri $hub_browser_auth_key {
    default "";
    ~^/(api/v3|testapi-staging|testapi)/auth/hub/(login|callback)/?$ $binary_remote_addr;
}
map $uri $hub_server_auth_key {
    default "";
    ~^/(api/v3|testapi-staging|testapi)/auth/hub/(token|refresh|revoke)/?$ $binary_remote_addr;
}
limit_req_zone $hub_browser_auth_key zone=hub_browser_auth:10m rate=30r/m;
limit_req_zone $hub_server_auth_key zone=hub_server_auth:10m rate=20r/s;
log_format api_safe escape=json
    '{"method":"$request_method","path":"$uri","status":$status}';
```

Add these directives to each relevant API proxy location, retaining its existing proxy settings:

```nginx
limit_req zone=hub_browser_auth burst=10 nodelay;
limit_req zone=hub_server_auth burst=100 nodelay;
limit_req_status 429;
access_log /var/log/nginx/api-access.log api_safe;
```

Empty map keys leave unrelated API routes unthrottled by these zones. Limits share state across workers on one Nginx instance, not across independent ingress instances. The token limit allows shared Hub egress; tune it from expected concurrency and measured traffic. Trust forwarded client IPs only from configured proxies. Check error logs too: Nginx error messages can include the original request URI even when access logs use a safe format. Redact them in the log pipeline before export. Validate with `nginx -t` and a staging burst test that verifies 429 responses and sanitized logs before deployment. See the native [Nginx limiter](https://nginx.org/en/docs/http/ngx_http_limit_req_module.html) and [logging](https://nginx.org/en/docs/http/ngx_http_log_module.html) documentation.

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

Before production, the Hub still needs shared protected session storage, coordination of refresh across instances, and verified CSRF defenses. Test browser refresh, logout, two-user isolation, and revocation through the deployed proxy. Hub tokens use current API permissions on every request and do not use the legacy API-key cache.

Account suspension remains a separate schema and policy change: the API currently has no disabled-account field. Revoking a Hub family ends that session but does not prevent a new ORCID login or revoke legacy keys. A future account suspension must reject new login, handoff exchange, refresh, and all authenticated requests, including legacy keys, and define cache invalidation before it is enabled. Do not present session revocation as account disablement.
