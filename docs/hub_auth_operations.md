# Hub authentication operations

How to run the API side of Hub sign-in: secrets, logging, the on/off switch, cleanup, and tests. The login flow and tables are described in [How Hub authentication works](hub_auth_overview.md). The Foundry's own values (secret names, cluster commands, Hub service) are in mf-crucible-deploy's `foundry/RUNBOOK.md`.

The routes use exact configured callbacks, OIDC validation, a one-use PKCE-bound handoff, and hashed API credentials. Code consumption and token issuance commit together. Refresh locks the token family and reloads its state under that lock; concurrent reuse revokes the family.

## Settings and secrets

- `HUB_AUTH_ENABLED=true` turns the routes on. When it is on, the startup check requires `HUB_AUTH_CLIENT_SECRET`, `HUB_AUTH_CALLBACK_URI`, and either `HUB_AUTH_ORCID_CLIENT_SECRET` or `ORCID_CLIENT_SECRET`.
- Each secret is given one way only: inline, `NAME_FILE`, a file named `NAME` in the secrets directory, or `NAME_VERSION` (a Secret Manager `projects/.../secrets/.../versions/N` or `versions/latest`). Giving one two ways stops the server at startup. Values must be one line of printable ASCII.
- Every worker reads its secrets at start and refuses to start if one cannot be read (`get_secret` in `app/config.py`). Successful reads are cached per worker; failures are not. Secret Manager lookups time out after ten seconds without retries, use Application Default Credentials, and stay in worker memory.
- The Hub client secret is shared only with the Hub server; the Hub never needs the ORCID client secret. Keep local environment files at mode `600`.
- `HUB_AUTH_CALLBACK_URI` must match the Hub's callback exactly; no wildcards. Register the API's `<public API URL>/auth/hub/callback` with ORCID.

Rotation adds a secret version, points the reference at it, and restarts the processes. Only one shared Hub client secret is accepted at a time, so coordinate rotation with the Hub. Workers keep cached values after a version is disabled: emergency revocation needs a restart and, where needed, token-family revocation.

## Logging

Responses under `/auth/hub`, including validation errors, redirects, and method errors, carry `Cache-Control: no-store`, `Pragma: no-cache`, and `Referrer-Policy: no-referrer`. Validation failures keep HTTP 422 and the `detail` shape with a fixed message that omits submitted values. Unexpected failures return a generic 500; logs keep only the exception class, status, route, and request ID.

Uvicorn access logging is off because it would record OAuth callback query strings; the request middleware logs method, route, status, duration, and request ID. Do not turn raw access logging back on. Configure proxies, load balancers, and tracing to omit query strings, authorization headers, cookies, bodies, and redirect `Location` headers.

## Turning sign-in off

Set `HUB_AUTH_ENABLED=false` and restart the API; environment changes do not reach running workers. This blocks new login, exchange, refresh, and revoke. Existing access tokens stay valid until they expire or are revoked, and API keys keep working. It is not an account-suspension switch. When a secret cannot be read, login and client endpoints answer a generic 503.

## Expired-record cleanup

`scripts/cleanup_hub_auth.py` deletes expired login transactions, handoff codes, and access tokens in bounded batches, and removes refresh records and families only after absolute family expiry (keeping spent-token history for replay detection). It never revokes a live session. Without `--commit` it is a read-only preview that prints counts.

```bash
DB_CONF_FILE=.env .venv/bin/python scripts/cleanup_hub_auth.py
DB_CONF_FILE=.env .venv/bin/python scripts/cleanup_hub_auth.py --commit --batch-size 1000 --batches 10
```

Run the commit form hourly with no overlapping runs, and watch its counts and duration. Revision `e92a6b1c730f` adds `(expires_at, primary key)` indexes for it, built concurrently. If that migration fails, inspect the four `ix_hub*_expiry` indexes and drop only those this revision created with `DROP INDEX CONCURRENTLY` before rerunning; never stamp it.

## Tests

PostgreSQL tests need an isolated local database named `crucible_hub_auth_test`; they refuse other names and non-loopback hosts. Never point them at a shared database.

```bash
HUB_AUTH_TEST_DATABASE_URL=postgresql://localhost/crucible_hub_auth_test \
  .venv/bin/pytest -q tests/test_hub_auth_postgres.py tests/test_hub_tokens.py
```

## Not yet supported

Accounts cannot be suspended: there is no disabled-account field. Revoking a token family ends one session but does not block a new ORCID login or API keys. Suspension needs its own schema and policy change covering login, exchange, refresh, every authenticated request, and cache invalidation.
