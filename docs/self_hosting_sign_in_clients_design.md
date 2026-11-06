# Sign-in for any client: design

Status: proposal, 2026-11-06. Nothing here is built. Each step is proposed to the user and built only after they agree.

## Why

The user decided on 2026-11-06 that the Hub is not a core part of Crucible: a deployment must work without it. Today it does not, in two ways:

- The only sign-in that works without the Hub is the API's own browser page, `/auth/login`, and it is ORCID only. Every other provider (Dex, Authentik, a lab's OIDC) signs people in only through the Hub flow under `/auth/hub/*`. A deployment with only Dex and no Hub has no sign-in at all.
- Creating named API keys, linking and unlinking logins, and changing a human administrator need a recent Hub session. Without the Hub, only the server CLI can do the administrator changes.

The user also wants other clients, first nano-crucible's CLI, to sign people in. The user chose option A on 2026-11-06: generalize what exists, using Authlib's authorization server parts, "as long as we follow best practices and develop a secure solution".

## What exists

The Hub sign-in is already a small OAuth 2.0 authorization server for one client:

- Authorization code with PKCE, S256 only (`/auth/hub/login`), `state`, an exact list of redirect addresses, `max_age` passed through to the provider, `required_user`, and `auth_time` returned.
- Code exchange at `/auth/hub/token` with HTTP Basic client authentication, one-minute single-use codes.
- Opaque access tokens (15 minutes) and refresh tokens (7 days idle, 30 days absolute) stored as hashes, in token families. Refresh rotates the token, and presenting a used refresh token revokes the whole family.
- Revocation at `/auth/hub/revoke`. Linking under `/auth/hub/link/*`.
- `client_id` is already stored on `hubtokenfamily`, `hublogintransaction` and `hublogincode`.

What ties it to the Hub: one `HUB_AUTH_CLIENT_ID`, one `HUB_AUTH_CLIENT_SECRET`, one list of redirect addresses, the `/auth/hub` paths, and `credential == "hub"` on the principal, checked in 7 places.

Provider sign-in (upstream OIDC through Authlib's client), linking, registration, pending accounts and the event log stay as they are.

## Standards this follows

- RFC 9700, OAuth 2.0 Security Best Current Practice: PKCE required for public clients and supported for all; exact redirect matching except the port of loopback addresses; refresh tokens for public clients sender-constrained or rotated; no implicit grant; the password grant "MUST NOT be used"; a mix-up defense; access tokens restricted to the minimum privileges and audience.
- RFC 8252, OAuth 2.0 for Native Apps: loopback redirects on `127.0.0.1` or `[::1]` with any port chosen at request time; `localhost` names are not recommended.
- RFC 8628, Device Authorization Grant: for clients with no browser, such as SSH sessions and cluster nodes.
- RFC 7009 (revocation), RFC 8414 (authorization server metadata), RFC 9207 (`iss` in the authorization response).
- OAuth 2.1 is still an Internet-Draft (`draft-ietf-oauth-v2-1-16`, September 2026). Its rules match RFC 9700, so following RFC 9700 keeps us compatible with it.
- NIST SP 800-63B-4 and the OWASP cheat sheets already used in Phase 3 for sessions, reauthentication and linking.

## Proposed design

### 1. Registered clients

A list of clients in settings, `CRUCIBLE_AUTH_CLIENTS`, in the same style as `CRUCIBLE_AUTH_PROVIDERS`. Each client has:

- `id` and `display_name`, shown on the sign-in and device pages.
- `type`: `confidential` (has a secret, runs on a server, like the Hub) or `public` (no secret, runs on people's machines, like the CLI).
- `redirect_uris`: exact addresses. Public clients may instead allow loopback (`http://127.0.0.1/<path>` and `http://[::1]/<path>`, any port).
- `grants`: which of `authorization_code`, `refresh_token` and `device_code` it may use.
- For confidential clients, the name of the secret in the secret store. Secrets are compared in constant time, as today.

Only the operator registers clients. There is no dynamic registration (RFC 7591), and no consent screen, since every client is first party. Third-party clients are out of scope.

The Hub becomes the first entry, built from today's `HUB_*` settings when `CRUCIBLE_AUTH_CLIENTS` is not set, so current deployments change nothing. A `nano-crucible` public client, with loopback redirects and the device grant, is suggested as a default entry in the Compose bundle, not hard-coded.

### 2. Grants

- Authorization code with PKCE S256, required for every client, public or confidential.
- Refresh with rotation and family revocation on reuse, as today. For public clients this satisfies RFC 9700's "sender-constrained or rotation" rule. DPoP (RFC 9449) sender-constraining is a possible later step.
- Device authorization grant for public clients that list it.
- Not offered: implicit, password, client credentials. Passwords (Tasks 10 to 13) are typed only into the API's own sign-in page, never sent to a client.

### 3. Routes

- `GET /oauth/authorize`, `POST /oauth/token`, `POST /oauth/revoke`, `POST /oauth/device_authorization`, and `GET`/`POST /oauth/device` (the page where a person enters the code).
- `GET /.well-known/oauth-authorization-server` (RFC 8414), so clients find the routes from the API address alone.
- The authorization response carries `iss` (RFC 9207).
- `/auth/hub/login`, `/token`, `/refresh` and `/revoke` stay as aliases on the same code, so the current Hub keeps working unchanged. Linking stays under `/auth/hub/link/*` for now, and moves to `/oauth/link/*` with aliases once a second client needs it.
- The provider callbacks (`/auth/hub/callback`, `/auth/hub/oidc/{key}/callback`) keep their addresses, because they are registered at each provider.

### 4. Tokens and sessions

- Access and refresh tokens stay opaque and stored as hashes, as today. No JWT access tokens: the API is its own only resource server, opaque tokens can be revoked at once, and there is no signing key to manage.
- Lifetimes per client type. Suggested: confidential clients keep 15 minutes, 7 days idle and 30 days absolute. Public clients get 15 minutes, 14 days idle and 90 days absolute, so a CLI on a cluster is not signed out every week. For the user to decide.
- The tables keep their names (`hubtokenfamily` and the others) to avoid a migration that renames them; the code calls them sessions.
- The principal's `credential` becomes `session` for any client, with `client_id` beside it. The checks for a recent Hub session become checks for a recent sign-in in any session (`auth_time` within 10 minutes, as today). The error codes `hub_session_required` and `insufficient_user_authentication` keep their names, so current clients understand them.
- With this, nano-crucible signed in through the device flow can create and revoke named keys, link logins and change administrators, with no Hub.
- `client_id` is recorded on `auth_event` for sign-ins and token issue, so people and administrators can see which client a session belongs to. A later `GET /account/sessions` with revoke can list them.

### 5. Device flow details

- User code: 8 characters from `BCDFGHJKLMNPQRSTVWXZ` shown as `WDJB-MJHT`, about 34.5 bits, generated with a CSPRNG (Authlib's default, which uses `random.SystemRandom`). Input ignores case, dashes and spaces. Device code: high entropy, stored as a hash, single use.
- Lifetime 10 minutes rather than RFC 8628's 30-minute example, to limit phishing (section 5.4). Polling every 5 seconds, with `slow_down` (`interval` grows by 5 seconds) for clients that poll too fast.
- Code entry is rate limited per client address and across all addresses, using the throttle table from Task 11. Until Task 11 is built, a simple per-address limit in the same table pattern.
- The device page shows the client's display name and says plainly that a program is asking to sign in as this person, to continue only if they started it themselves (RFC 8628 section 5.4). The person must sign in through a provider on that page; the device is approved only after a sign-in no older than 10 minutes, so approving a device always needs a fresh sign-in, the same rule as creating a key.
- The page asks the person to confirm the code even when it arrives pre-filled through `verification_uri_complete`.
- Pending, disabled and rejected accounts get the same pages and errors as in the Hub flow.

### 6. Built on Authlib

Authlib 1.9.1, already a dependency, has framework-independent server classes: `AuthorizationServer`, `AuthorizationCodeGrant` with `CodeChallenge` (PKCE), `RefreshTokenGrant`, `DeviceCodeGrant` and `DeviceAuthorizationEndpoint`, `RevocationEndpoint`, `IssuerParameter` (RFC 9207) and metadata (RFC 8414). Its ready-made server integrations are only for Flask and Django; a FastAPI one was proposed (lepture/authlib PR #278) and not merged, and the project lists FastAPI integration as planned work.

So the API needs a small adapter: subclass `AuthorizationServer` and implement `query_client`, `save_token`, `send_signal`, `create_oauth2_request`, `create_json_request` and `handle_response` for Starlette requests. Authlib's own Flask adapter is about 220 lines, which sets the size. Authlib's server is synchronous, so these routes are sync FastAPI routes, run in the thread pool, as most of the API already is. Authlib's version is pinned, and its release notes are checked on each upgrade.

What stays ours: the upstream provider sign-in (already on Authlib's client), the token storage and families, the device page, linking, registration and the throttle. Authlib supplies the protocol checks: parameters, PKCE, redirect matching, grant rules and error responses.

### 7. Library choice

Compared on 2026-11-06, and the user chose to stay with Authlib:

- aioauth (2.0.1, July 2026) is the only one written for async frameworks, but has no device grant, revocation or metadata.
- oauthlib (4.0.1, October 2026) is mature and has the device grant, but would be a second OAuth library beside Authlib, with a large validator interface to implement and the same FastAPI glue.
- Ory Hydra (an external service, device grant since v25.4.0) would leave us no protocol code, at the cost of another service and database in every deployment. Worth revisiting if the protocol code grows.
- Authlib's server core is synchronous, which costs nothing here: the API's routes are mostly synchronous already. Upgrade from 1.9.1 to the current release in its own pull request before building on it, reading the release notes.

### 8. Provider tokens

Crucible keeps no provider tokens: sign-in checks the provider's ID token, keeps who the person is, and discards the provider's access and refresh tokens. If Crucible ever has to call a provider's API for a person, such as ORCID's member API, those refresh tokens must be stored server-side and encrypted with a key kept apart from database backups, like the password pepper. Hashing is not enough for them, because they must be readable to be used.

## Compatibility

- The current Hub keeps working with no change: same routes (as aliases), same client ID and secret, same token responses.
- Released nano-crucible keeps using API keys. API keys stay; this adds sessions beside them.
- Production on 3.3.1 is unaffected until the Phase 3 release.
- Clients find the routes through the metadata document, so a nano-crucible release can check for it and fall back to API keys against an older API.

## Steps

Small pull requests, each deployable alone:

1. Client list in settings, with the Hub as entry one, and the principal's `session` credential with `client_id`. No behavior change; current tests pass unchanged.
2. Authlib adapter, and the Hub's code exchange, refresh and revocation moved onto it with the same responses. The current Hub tests act as the contract.
3. `/oauth/*` routes, the metadata document and `iss`, with `/auth/hub/*` as aliases.
4. Public clients: loopback redirects, per-type lifetimes, the `nano-crucible` entry in the Compose bundle.
5. Device flow and its page, with the rate limit.
6. The nano-crucible session builds the CLI sign-in; the Hub session is told about the generic routes. Docs and CHANGELOG.

Order in Phase 3: after Task 9 step 2, before Task 12, so the password form (Task 12) is built once, on the generic sign-in page, and not first on `/auth/hub`. Tasks 10 and 11 (password hashing and attempt limits) do not depend on it and can go either side.

## Open for the user

1. Scopes. RFC 9700 says access tokens SHOULD carry the least privilege needed. API keys today carry the person's full power, and so would sessions at first. A `read` scope for scripts and notebooks would be the first candidate, later.
2. Session lifetimes for public clients (suggested above).
3. Whether a `nano-crucible` client is in the Compose bundle by default.
4. The API's own key page (`/auth/apikey`): keep it as it is until Task 9 step 3, then rebuild it on the generic sign-in, or retire it once the Hub and CLI cover it.

## Sources

- RFC 9700, OAuth 2.0 Security Best Current Practice: https://www.rfc-editor.org/rfc/rfc9700.html
- RFC 8628, OAuth 2.0 Device Authorization Grant: https://www.rfc-editor.org/rfc/rfc8628.html
- RFC 8252, OAuth 2.0 for Native Apps: https://datatracker.ietf.org/doc/html/rfc8252
- RFC 8414, RFC 7009, RFC 9207, RFC 9449 on rfc-editor.org
- OAuth 2.1 draft 16: https://www.ietf.org/archive/id/draft-ietf-oauth-v2-1-16.txt
- Authlib FastAPI server proposal: https://github.com/lepture/authlib/pull/278
- Authlib project page: https://authlib.org/projects/authlib/
- aioauth: https://pypi.org/project/aioauth/ ; oauthlib: https://pypi.org/project/oauthlib/ ; Ory v25.4.0 device grant: https://changelog.ory.com/announcements/ann_Dne2mookJyqgU
- Authlib 1.9.1 source: `authlib/oauth2/rfc6749/authorization_server.py`, `authlib/oauth2/rfc8628/`, `authlib/integrations/flask_oauth2/`
