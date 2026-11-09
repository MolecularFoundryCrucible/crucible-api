# Hub authentication operations

How to run the API side of Hub sign-in: secrets, logging, the on/off switch, cleanup, and tests. The login flow and tables are described in [How Hub authentication works](hub_auth_overview.md). The Foundry's own values (secret names, cluster commands, Hub service) are in mf-crucible-deploy's `foundry/RUNBOOK.md`.

The routes use exact configured callbacks, OIDC validation, a one-use PKCE-bound handoff, and hashed API credentials. Code consumption and token issuance commit together. Refresh locks the token family and reloads its state under that lock; concurrent reuse revokes the family.

## Settings and secrets

- `CRUCIBLE_AUTH_CLIENTS` is the JSON list of programs that sign people in through the API, such as the Hub. See [the client list](#the-client-list). Without it, `HUB_AUTH_ENABLED=true` registers the Hub alone from the older settings: the startup check then requires `HUB_AUTH_CLIENT_SECRET`, `HUB_AUTH_CALLBACK_URI`, `CRUCIBLE_AUTH_PROVIDERS` with at least one provider, and each listed provider's secret. Setting both stops the server at startup. The older settings are kept for now and will be removed in a later major version.
- `CRUCIBLE_AUTH_IDENTITY_LOOKUP` makes ORCID sign-in find people only through `user_identity`. Turn it on after running `migrations/data/20261103_backfill_user_identity.py`; with it on, someone whose `unique_id` is an ORCID but who has no row is refused. It needs a restart to change.
- `CRUCIBLE_AUTH_PROVIDERS` is the JSON list of sign-in providers. Nothing is implied from other settings, and the older `ORCID_*` settings serve only `/auth/login`. See [the provider list](#the-provider-list).
- Each secret is given one way only: inline, `NAME_FILE`, a file named `NAME` in the secrets directory, or `NAME_VERSION` (a Secret Manager `projects/.../secrets/.../versions/N` or `versions/latest`). Giving one two ways stops the server at startup. Values must be one line of printable ASCII.
- Every worker reads its secrets at start and refuses to start if one cannot be read (`get_secret` in `app/config.py`). Successful reads are cached per worker; failures are not. Secret Manager lookups time out after ten seconds without retries, use Application Default Credentials, and stay in worker memory.
- The Hub client secret is shared only with the Hub server; the Hub never needs the ORCID client secret. Keep local environment files at mode `600`.
- `HUB_AUTH_CALLBACK_URI` must match the Hub's callback exactly; no wildcards. Register the API's `<public API URL>/auth/hub/callback` with ORCID.

## Passwords

Passwords are meant for small local deployments; institutions should sign people in through their own provider (OIDC). With `CRUCIBLE_AUTH_PASSWORD_ENABLED=true`, the sign-in choice offers a password beside the listed providers (or goes straight to it when none is listed), and `/oauth/password` shows the form. A correct password returns to the client like any provider sign-in, with `code`, `state` and `iss`, and the sign-in history records the API itself as the issuer. Every failure gives the same page, status and time. The server refuses to start with passwords on unless the public API address is https (http only on localhost). The form's CSP allows posting to itself and the redirect to the client's origin, which Chrome otherwise blocks; `tests/test_auth_password_browser.py` checks this in headless Chromium and Firefox. An account with a password cannot clear its username.

- Storage: Argon2id through `pwdlib`, in `local_credential`, apart from the user table. The password is NFC-normalized, then HMAC-SHA256 with the pepper and base64-encoded, then hashed. `CRUCIBLE_AUTH_ARGON2_MEMORY_KIB`, `_TIME_COST` and `_PARALLELISM` default to 64 MiB, 3 and 1; the server refuses settings below OWASP's floor (19 MiB with 2 iterations, or an equal pair). A hash made with older settings is replaced at the next successful sign-in.
- Pepper: `CRUCIBLE_AUTH_PEPPER`, a secret of at least 32 characters given like any other, named by `CRUCIBLE_AUTH_PEPPER_ID`. With passwords on, the server refuses to start without both. Back it up apart from database backups: a database leak alone then reveals nothing usable, but losing the pepper invalidates every password (API keys keep working, and an administrator can issue new links). To rotate, give a new pepper and ID, list the old ID in `CRUCIBLE_AUTH_PREVIOUS_PEPPER_IDS`, and keep its secret as `CRUCIBLE_AUTH_PREVIOUS_PEPPER_<ID>` until everyone has signed in again.
- Attempt limits (NIST SP 800-63B-4 3.2.2, OWASP): each attempt is counted before the password is hashed, in one atomic update, and a success gives it back. Unknown browsers get 4 failures without delay, then 2, 4, 8 seconds up to 15 minutes, and are refused at 90; an attempt during a delay is not checked or counted. A browser that signed in before (a signed device cookie, `CRUCIBLE_AUTH_DEVICE_COOKIE_DAYS`, 90 by default, cancelled by any password change) or a signed-in session has no delays, but 10 failures disable the password until an administrator issues a link. Never more than 100 in all. Per client address, across accounts, 10 failures are free, then the same delays; the count restarts after an hour without failures. Addresses are stored only as keyed hashes, and rows go after 30 days. `CRUCIBLE_AUTH_HASH_CONCURRENCY` (2) bounds the checks running at once in each worker.
- Client addresses come from gunicorn and uvicorn, which read `X-Forwarded-For` only from the proxies in `FORWARDED_ALLOW_IPS`. A deployment with passwords on must set it to its proxy, as the Compose bundle does; otherwise every request appears to come from the proxy and one attacker delays everyone.
- Rules: at least `CRUCIBLE_AUTH_PASSWORD_MIN_LENGTH` characters (15 by default, as NIST SP 800-63B-4 asks for single-factor passwords; 12 at the lowest), at most 256, counted in Unicode code points. No rules about character kinds. A password matching a list of about 30,000 common ones (from SecLists, `app/auth/data/blocked_passwords.txt`, rebuilt by `scripts/build_password_blocklist.py`), or the person's username, email or names, is refused with the reason.
- Registration: with `CRUCIBLE_AUTH_PASSWORD_REGISTRATION=approval`, `/oauth/register` lets people ask for an account with a username, names, an email and a password, and the password sign-in page links to it. The account waits until an administrator approves it, and unapproved ones go after 30 days. Administrators list waiting accounts with `GET /users?pending=true` or `python -m app.cli admin pending`, see each one's details and `sign_in_methods` (`password` or the provider) with `GET /users/{user_ref}`, and decide with `POST /users/{user_ref}/approve` or `/reject`, or `admin approve USER` and `admin reject USER` on the server. Nothing notifies them yet. `GET /auth/methods` lists `password` when passwords are on, with `registration_url` while registration is open, so clients can show the link without probing the page. The email is required but not verified, so nothing trusts it. A username or email already in use gets one shared answer, and every submission counts against the per-address limit. The default is `closed`; `open` is refused at startup, since nothing verifies who signed up.
- Sign-in history: `auth_event` records sign-ins, each checked wrong password and the lock it causes, password and login changes, keys, approval, disabling and ended sessions, kept `CRUCIBLE_AUTH_EVENT_RETENTION_DAYS` (365 by default). No client address is stored. Administrators read a person's history with `GET /users/{user_ref}/auth-events`.
- Server commands: `admin create` prints a one-time `password_link` beside the API key when passwords are on, so the first administrator can set a password; `admin password-link USER` issues one for anyone, including a locked-out administrator. After a breach, `auth revoke-passwords --commit` removes every password, cancels the links and ends those people's sessions; people with another login keep it, and the rest need a new link. API keys are separate: revoke them with `DELETE /users/{user_ref}/apikey`. When a provider's issuer address changes, list the new issuer in `CRUCIBLE_AUTH_PROVIDERS`, restart, then run `auth rewrite-issuer --from OLD --to NEW --commit`. Every command reports counts and changes nothing without `--commit`.

## The client list

Every field is required, and an unknown field stops the server at startup. A client's secret is `OAUTH_{KEY}_CLIENT_SECRET`, given like any other secret. Client secrets, including the older `HUB_AUTH_CLIENT_SECRET`, must be at least 32 characters, or the server refuses to start; `openssl rand -hex 32` gives 64.

```json
[{"key": "hub", "client_id": "crucible-hub", "display_name": "Crucible Hub", "type": "confidential",
  "redirect_uris": ["https://hub.example.org/hub/auth/callback", "https://hub.example.org/hub/auth/link/callback"]}]
```

| Field | Meaning |
|---|---|
| `key` | Lowercase letters and digits; names the secret |
| `client_id` | What the client sends as its HTTP Basic user name |
| `display_name` | Shown to people when the client asks to sign them in |
| `type` | `confidential` (has a secret, runs on a server, like the Hub) or `public` (runs on people's computers, like the nano-crucible command line, and has no secret) |
| `redirect_uris` | Exact addresses the client may receive codes at: https, or http only on `localhost`, `127.0.0.1` and `[::1]`, with no user, query or fragment. A public client's `http://127.0.0.1/<path>` or `http://[::1]/<path>`, written without a port, accepts any port at sign-in (RFC 8252 7.3); only public clients may use that form |

Codes, sessions and links belong to the client that started them, and no other client can use them. Removing a client from the list ends its sessions at once.

Device sign-in (RFC 8628), for a public client on a machine without a browser such as an SSH session: the program calls `POST /oauth/device_authorization` with its `client_id` and shows the person a code such as `WDJB-MJHT` and the address `<public API address>/oauth/device`. On any device with a browser, the person enters the code, signs in afresh (`max_age=0`) through the usual choice of provider or password, and approves on a page naming the program, the account and when it asked. The program polls `/oauth/token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code` and gets the standard answers (`authorization_pending`, `slow_down`, `access_denied`, `expired_token`) until tokens arrive, once. Codes last 10 minutes, are stored only as hashes (the short code as a keyed hash), and wrong codes count against the per-address attempt limit. The cleanup job removes rows a day after they expire. The approval page signs people in as the API's own internal client, `crucible-device-approval`, which never receives tokens.

Sessions: every sign-in through a client (the Hub, the command line on a laptop, an approved device) is its own session, and a person may have any number (OWASP ASVS 5.0 V7.1.2). `GET /account/sessions` lists them, never their tokens, with `current` marking the one asking. Ending one (`DELETE /account/sessions/{id}`) or all but the current one (`DELETE /account/sessions`) needs a session with a sign-in in the last 10 minutes, not an API key (ASVS V7.5.2). Administrators list and end a person's sessions at `/users/{user_ref}/sessions` (V7.4.5); another human administrator's need a recent sign-in. `python -m app.cli auth end-sessions --user REF` or `--all`, a dry run unless `--commit`, ends them from the server, for example after a breach. Ending sessions never touches API keys.

A public client has no secret: it sends its `client_id` to `/oauth/token` and `/oauth/revoke`, and PKCE proves the code is its own (RFC 9700). It cannot use the Hub's `/auth/hub/token` routes, which need a secret. Because its identity cannot be verified, the person confirms on the API's own page that the program on their computer may sign in, before the code is sent (RFC 8252 8.6); its codes wait 5 minutes instead of 1. Its sessions last 14 days without use and 90 days at most, against 7 and 30 for confidential clients.

`GET /oauth/authorize` is the standard authorization endpoint (`response_type=code`, PKCE with S256, an exact `redirect_uri` and `state`); `/auth/hub/login` is the same handler. Linking a login uses `POST /account/identities/{provider}/link` and then `/oauth/link/start`, `/oauth/link/preview` and `/oauth/link/confirm`. Every answer back to a client, codes and errors alike, carries `iss`, the public API address (RFC 9207). Unknown clients and redirects get 400 and are never redirected to. The metadata (RFC 8414) is at `<public API address>/.well-known/oauth-authorization-server`, under the API rather than the host root, so clients are told where to look; its `issuer` equals `iss`.

`POST /oauth/token` (RFC 6749: `authorization_code` with PKCE, or `refresh_token`) and `POST /oauth/revoke` (RFC 7009) are the standard token routes, form-encoded, with HTTP Basic client authentication. The Hub's `/auth/hub/token`, `/refresh` and `/revoke` run the same server through Authlib and keep their JSON shapes. Requests are checked against the public API address, so it must be https outside localhost.

## The provider list

[`deploy/compose/auth-providers.example.json`](../deploy/compose/auth-providers.example.json) is a complete example. Every field is required, and an unknown field stops the server at startup.

| Field | Meaning |
|---|---|
| `key` | Short name, lowercase letters and digits, starting with a letter. It names the secret. |
| `display_name` | The name people see. |
| `issuer` | The provider's exact issuer, compared character by character with its discovery document, so keep any trailing slash it has (Authentik has one). `https://`, or `http://` only for `localhost`. |
| `client_id` | The client ID the provider gave you. |
| `scopes` | Space-separated scopes, including `openid`. ORCID uses `openid`. |
| `token_endpoint_auth_method` | `client_secret_post` for ORCID; otherwise usually `client_secret_basic`. |
| `registration` | `closed`, `approval` or `open`. Stored now, enforced once registration is built. Until then only ORCID creates accounts; other providers sign in only people already in `user_identity`, and anyone else gets `access_denied`. |
| `legacy_callback` | Uses `<public API URL>/auth/hub/callback`, for an ORCID client already registered with that address. ORCID only, and at most one provider. Otherwise the callback to register with the provider is `<public API URL>/auth/hub/oidc/{key}/callback`. |

A provider whose issuer is exactly `https://orcid.org` or `https://sandbox.orcid.org` is the ORCID provider, and only one may be listed. The client secret never goes in the list. It is `OIDC_{KEY}_CLIENT_SECRET`, for example `OIDC_ORCID_CLIENT_SECRET`, given any of the ways above.

A provider whose discovery document cannot be read or does not match is unavailable; other providers keep working, and it is tried again after 30 seconds. Discovery is read on first use, so this never stops the server from starting.

## How the Hub signs in

`GET /auth/methods` lists the ways to sign in. `key` is whatever the deployment chose, so use `is_orcid` to show the ORCID mark. `GET /auth/hub/login` takes an optional `provider` key. Without one, a single listed provider is used directly, and several show a page to choose. `required_user` names the `unique_id` that must sign in; it passes through the browser, so the Hub must still compare the account it receives.

Once a sign-in request is valid, failures redirect to the Hub's callback with `state` and an RFC 6749 `error`:

| `error` | Meaning |
|---|---|
| `access_denied` | The person refused at the provider, their login is not linked to an account, or the login is refused for this account |
| `login_required` | Someone other than `required_user` signed in; sent for nothing else |
| `temporarily_unavailable` | The provider cannot be reached or its discovery document is bad; try again |
| `server_error` | The provider's answer did not verify |

A callback that cannot be matched to this browser's sign-in gets the API's own 400 page, because its redirect target cannot be trusted. `POST /auth/hub/token` and `POST /auth/hub/refresh` return `auth_time`, null when the provider gave none.

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
HUB_AUTH_TEST_DATABASE_URL=postgresql+psycopg2://localhost/crucible_hub_auth_test \
  .venv/bin/pytest -q tests/test_hub_auth_postgres.py tests/test_hub_tokens.py
```

## Not yet supported

Administrators can switch an account off with `POST /users/{user_ref}/disable`, which ends its Hub sessions and blocks new sign-ins; API keys stop within the 60-second cache lifetime on other workers. Revoking a token family ends one session but does not block a new sign-in or API keys.
