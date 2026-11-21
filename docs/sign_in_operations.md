# Authentication configuration

This page is for operators. Client-side usage is covered in [Authentication](sign_in.md). The Docker Compose bundle configures most of these settings; see [Sign-in methods](https://github.com/MolecularFoundryCrucible/crucible-selfhost/blob/main/compose/docs/sign_in.md) in crucible-selfhost.

## Settings overview

| Setting | Description |
|---|---|
| `API_PUBLIC_URL`, `API_PATH` | The address used by users and clients. Authentication requires HTTPS, except on `localhost`. |
| `CRUCIBLE_AUTH_PROVIDERS` | The identity providers users sign in with: [the provider list](#the-provider-list) |
| `CRUCIBLE_AUTH_CLIENTS` | The client applications permitted to authenticate users: [the client list](#the-client-list) |
| `CRUCIBLE_AUTH_PASSWORD_ENABLED` | Password authentication; disabled unless set. See [Passwords](#passwords). |
| `CRUCIBLE_AUTH_IDENTITY_LOOKUP` | Resolves users by their recorded identities. Enabled on a new installation; an upgraded installation enables it after `python -m app.cli identity backfill`. Temporary: it will become permanently enabled. |
| `CRUCIBLE_AUTH_EVENT_RETENTION_DAYS` | Retention period of the sign-in history, in days (365) |
| `FORWARDED_ALLOW_IPS` | The proxies whose `X-Forwarded-For` header is trusted: addresses or ranges (`10.0.0.0/20`), comma separated, as seen by the API on the incoming connection. Attempt limits are counted per client address, so behind a proxy this must be set, or all requests share one address. The proxy must set the header to the client address rather than append to the value sent by the client; `*` is safe only when no host other than that proxy can reach the API. |

Authentication is enabled when at least one client is registered, and requires at least one identity provider or password authentication. With no client registered, only API keys are accepted.

## Secrets

- Supply each secret through exactly one source: `NAME`, `NAME_FILE`, a file named `NAME` in the secrets directory (`/run/secrets`), or `NAME_VERSION` (a Google Secret Manager version). Two sources stop the server at startup.
- A value is one line of printable ASCII. Client secrets and the pepper require at least 32 characters; `openssl rand -hex 32` produces 64.
- Secrets are read at startup. Changing one requires a restart.

## The provider list

`CRUCIBLE_AUTH_PROVIDERS` is a JSON list. Every field below is required, and an unknown field stops the server at startup. [`auth-providers.example.json`](https://github.com/MolecularFoundryCrucible/crucible-selfhost/blob/main/compose/auth-providers.example.json) is a complete example.

| Field | Description |
|---|---|
| `key` | Short name of lowercase letters and digits. It determines the secret name and the callback URL. |
| `display_name` | The name shown to users |
| `issuer` | The provider's exact issuer identifier, compared character by character with its discovery document; keep a trailing slash if present |
| `client_id` | The client ID issued by the provider |
| `scopes` | Space-separated, including `openid` |
| `token_endpoint_auth_method` | `client_secret_post` for ORCID; otherwise usually `client_secret_basic` |
| `registration` | Handling of a new user: `closed` refuses them, `approval` creates an account pending administrator approval, `open` admits them |

An older list may still carry `"legacy_callback": false`, which is accepted and ignored; `true` stops the server, since `<API>/auth/hub/callback` no longer exists.

- The provider's secret is `OIDC_{KEY}_CLIENT_SECRET`, for example `OIDC_ORCID_CLIENT_SECRET`.
- Register `<API>/oauth/callback/{key}` with the provider as the redirect URI.
- A provider with issuer `https://orcid.org` or `https://sandbox.orcid.org` is the ORCID provider; at most one may be listed.
- An unreachable provider is skipped and retried after 30 seconds; the other providers remain available.
- When a provider's issuer changes: list the new issuer, restart, then run `python -m app.cli auth rewrite-issuer --from OLD --to NEW --commit`.

## The client list

`CRUCIBLE_AUTH_CLIENTS` is a JSON list. Every field is required.

```json
[{"key": "hub", "client_id": "crucible-hub", "display_name": "Crucible Hub", "type": "confidential",
  "redirect_uris": ["https://hub.example.org/hub/auth/callback", "https://hub.example.org/hub/auth/link/callback"]},
 {"key": "nanocrucible", "client_id": "nano-crucible", "display_name": "nano-crucible", "type": "public",
  "redirect_uris": ["http://127.0.0.1/callback", "http://[::1]/callback"]}]
```

| Field | Description |
|---|---|
| `key` | Lowercase letters and digits; determines the secret name |
| `client_id` | The identifier the client sends |
| `display_name` | Shown to users when the client requests authentication |
| `type` | `confidential`: runs on a server and holds a secret, `OAUTH_{KEY}_CLIENT_SECRET`. `public`: runs on users' computers or phones and holds none. |
| `redirect_uris` | The exact URIs at which the client may receive authorization codes: `https`, or `http` on `localhost`, `127.0.0.1` and `[::1]`. For a public client, `http://127.0.0.1/<path>` written without a port accepts any port. |

- Removing a client from the list ends its sessions immediately.
- A public client may also use the device authorization grant, for hosts without a browser.
- The legacy `HUB_AUTH_*` settings still register the Hub alone when no client list is given. Setting both stops the server. They will be removed.

## Passwords

Intended for small installations; an institution should use its own identity provider.

- Enable with `CRUCIBLE_AUTH_PASSWORD_ENABLED=true`, a secret `CRUCIBLE_AUTH_PEPPER` and its identifier `CRUCIBLE_AUTH_PEPPER_ID`. The server refuses to start without them, or over plain HTTP other than on `localhost`.
- **Back up the pepper separately from the database.** A leaked database is unusable without it; losing it invalidates every password.
- An administrator creates each user and issues a one-time link: `POST /users/{user_ref}/password-link`, or `python -m app.cli admin password-link USER`. Links are valid for `CRUCIBLE_AUTH_PASSWORD_LINK_HOURS` (24).
- `CRUCIBLE_AUTH_PASSWORD_REGISTRATION=approval` allows users to request an account at `/oauth/register`. Each request awaits an administrator: `GET /users?pending=true`, then `POST /users/{user_ref}/approve` or `/reject`. Requests pending for 30 days are removed, and requests are refused while `CRUCIBLE_AUTH_MAX_PENDING_ACCOUNTS` accounts wait.
- Passwords require 15 characters (`CRUCIBLE_AUTH_PASSWORD_MIN_LENGTH`), with no character class rules; common passwords and the user's own name or email are rejected.
- Failed attempts are throttled, then blocked, as listed under [Limits](#limits).
- To rotate the pepper: supply a new pepper and identifier, list the old identifier in `CRUCIBLE_AUTH_PREVIOUS_PEPPER_IDS`, and keep its secret as `CRUCIBLE_AUTH_PREVIOUS_PEPPER_<ID>` until all users have signed in again.

## Limits

Each limit applies per account, per network address (an IPv4 address, or the /64 network of an IPv6 address), or per API worker. The adjustable ones are settings; the per-account limits are fixed, because loosening them weakens protection against password guessing.

| Limit | Applies to | Value | Setting |
|---|---|---|---|
| Failures before an unknown browser is delayed | Account | 4; delays then double from 2 s to 15 min | Fixed |
| Failures before an unknown browser is refused | Account | 90 | Fixed |
| Failures from a known browser before the password is disabled | Account | 10; an administrator issues a new link | Fixed |
| Failures in total before the password is disabled | Account | 100 | Fixed |
| How long a browser stays known after a password sign-in | Browser | 90 days | `CRUCIBLE_AUTH_DEVICE_COOKIE_DAYS` |
| Failed password and code attempts before an address is delayed | Address | 10, counted while failures come less than an hour apart | `CRUCIBLE_AUTH_ADDRESS_FREE_FAILURES` |
| Longest delay for an address | Address | 60 s; delays double from 2 s | `CRUCIBLE_AUTH_LONGEST_ADDRESS_DELAY_SECONDS` |
| Password checks at once | Worker | 2, each holding `CRUCIBLE_AUTH_ARGON2_MEMORY_KIB` of memory | `CRUCIBLE_AUTH_HASH_CONCURRENCY` |
| Password checks waiting for a slot | Worker | 8; beyond that, a check is refused at once as busy | `CRUCIBLE_AUTH_HASH_QUEUE` |
| Longest wait for a slot | Worker | 10 s, then the check is refused as busy | `CRUCIBLE_AUTH_HASH_WAIT_SECONDS` |
| Accounts waiting for approval | Installation | 100; registration is refused while the list is full | `CRUCIBLE_AUTH_MAX_PENDING_ACCOUNTS` |
| How long a waiting account is kept | Account | 30 days | Fixed |
| Active API keys | Person | 10 | Fixed |

A known browser is exempt from the address limit. An attempt refused because its address is delayed, or because no slot is free, is not counted against the account.

## Administrative commands

Run as `python -m app.cli <command>`. Commands that change data report the planned changes and change nothing without `--commit`.

| Command | Description |
|---|---|
| `admin create` | Create an administrator; prints an API key and, with password authentication enabled, a password link |
| `admin set-role` | Change a user's platform role |
| `admin password-link USER` | Issue a one-time password link, including for a locked-out administrator |
| `admin pending`, `admin approve USER`, `admin reject USER` | Review accounts pending approval |
| `auth cleanup` | Remove expired authentication records; run hourly |
| `auth end-sessions --user REF` or `--all` | End sessions, for example during incident response |
| `auth revoke-passwords` | Remove every password and end the associated sessions |
| `auth rewrite-issuer` | Move identities to a provider's new issuer |
| `identity backfill` | Record an identity for each user created before 4.0.0 |
| `identity move` | Move an identity off an empty duplicate account |

API keys are independent of sessions and passwords: revoke them with `DELETE /users/{user_ref}/apikeys/{key_id}`.

## Incident response

1. `auth end-sessions --all --commit` ends every session.
2. With password authentication: `auth revoke-passwords --commit`, then issue new links.
3. Revoke the affected API keys.
4. Replace the compromised secret and restart. A replaced client secret requires updating the client at the same time.

## Disabling authentication

Remove every client from the list (and unset `HUB_AUTH_ENABLED`), then restart. New sign-ins are refused; access tokens already issued remain valid until they expire, at most 15 minutes; API keys remain valid. To block a single user, disable the account with `POST /users/{user_ref}/disable`.

## Logging and reverse proxies

- The API never logs query strings, request bodies, keys or tokens, and its own access log is disabled, because authentication callbacks carry one-time codes in the URL.
- Configure every reverse proxy, load balancer and tracer in front of the API the same way: no query strings, `Authorization` headers, cookies or redirect `Location` headers in logs.
- Limit authentication routes (`/oauth/*`) to small request bodies; the Caddy configuration in crucible-selfhost allows 8 KB.
