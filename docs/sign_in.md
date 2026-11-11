# Authentication

This page is for developers of client applications. Server configuration is covered in [Authentication configuration](sign_in_operations.md).

## Components

- **A user** has one account and one or more linked identities: an ORCID iD, an institutional account, a password. The available sign-in methods depend on the deployment; `GET /auth/methods` lists them.
- **A client** is an application registered to authenticate users: the Hub, the nano-crucible command-line interface, Crucible Lens. The operator registers each one. A client running on a server holds a secret (`confidential`); a client running on a user's computer or phone holds none (`public`).
- **A session** is one authentication through one client. It issues the client an access token and a refresh token.
- **An API key** is a long-lived credential that a user creates for scripts and instruments. Service accounts authenticate with keys only.

Both credentials are sent in the same header: `Authorization: Bearer <access token or API key>`.

## Authorization code flow

Crucible is a standard OAuth 2.0 authorization server with PKCE. `GET <API>/.well-known/oauth-authorization-server` lists the endpoints below.

1. Redirect the user's browser to `GET /oauth/authorize` with `response_type=code`, the `client_id`, the exact `redirect_uri`, a random `state` (16 to 256 characters), and `code_challenge` with `code_challenge_method=S256`. Optional parameters: `provider` to skip the provider selection page, `max_age=0` to force reauthentication, `required_user` to require a specific account.
2. The user signs in. For a public client, sign-in ends on a consent page where the user confirms the client application.
3. The browser returns to the `redirect_uri` with `code`, `state` and `iss`, or with `error`. Verify that `state` matches the value sent and that `iss` is the API's address.
4. Exchange the code at `POST /oauth/token`, form-encoded: `grant_type=authorization_code`, `code`, `redirect_uri`, `code_verifier`. A confidential client authenticates with HTTP Basic; a public client sends `client_id`.

The response:

```json
{"access_token": "...", "token_type": "Bearer", "expires_in": 900,
 "refresh_token": "...", "refresh_expires_in": 2592000,
 "user_unique_id": "...", "auth_time": 1762800000}
```

| | Confidential client | Public client |
|---|---|---|
| Access token lifetime | 15 minutes | 15 minutes |
| Session idle timeout (no refresh) | 7 days | 14 days |
| Maximum session lifetime | 30 days | 90 days |
| Authorization code lifetime | 1 minute | 5 minutes |

## Session lifecycle

- **Refresh** before the access token expires: `POST /oauth/token` with `grant_type=refresh_token` and `refresh_token`. Refresh tokens are rotated: every refresh returns a new refresh token, which replaces the previous one.
- **Serialize refreshes.** Reuse of a rotated refresh token ends the entire session (`invalid_grant`). The token replaced most recently is accepted for 30 seconds, so two concurrent refreshes do not end the session.
- **Sign out** with `POST /oauth/revoke` and `token`. This ends the session only, not the user's other sessions or API keys.
- **Errors** follow RFC 6749: `{"error": "...", "error_description": "..."}` with 400 or 401.

Users list and end their sessions with `GET` and `DELETE /account/sessions`. Ending all sessions also cancels authorizations that were approved but not completed: an authorization code not yet exchanged, or a device authorization whose tokens have not been retrieved.

## Device authorization flow

For a public client on a remote terminal:

1. `POST /oauth/device_authorization` with `client_id`. The response contains a `user_code`, a `verification_uri` and a `device_code`.
2. Display the code and the verification address to the user. The user opens the address on any device, signs in, and approves the request.
3. Poll `POST /oauth/token` with `grant_type=urn:ietf:params:oauth:grant-type:device_code`, `device_code` and `client_id`, every `interval` seconds, until tokens are issued. Until then the response is `authorization_pending`, `slow_down`, `access_denied` or `expired_token`. Device codes expire after 10 minutes.

## Authorization errors

When authentication fails after a valid authorization request, the browser returns to the `redirect_uri` with `state` and one of:

| `error` | Meaning |
|---|---|
| `access_denied` | The user declined, or the identity is not linked to an account |
| `login_required` | A user other than `required_user` signed in |
| `temporarily_unavailable` | The identity provider is unreachable; retry later |
| `server_error` | The identity provider's response failed verification |

A request with an unknown client or redirect URI receives a 400 page and is never redirected.

## Operations that require recent browser authentication

Creating an API key, ending sessions, changing a user's sign-in methods, and changing administrators require a session authenticated at most ten minutes earlier.

| Caller | Response |
|---|---|
| An API key | 403 `hub_session_required` |
| A session from the device authorization flow | 403 `browser_sign_in_required` |
| A session with an older authentication | 401 `insufficient_user_authentication`: redirect the user to `/oauth/authorize` with `max_age=0` |

An administrator's session acts with ordinary privileges unless a request carries `Crucible-Privilege-Mode: elevated`. An administrator's API key acts elevated when the header is omitted. See [explicit privilege mode](explicit_privilege_mode_design.md).

## API keys

- `POST /account/apikeys` creates a named key and returns it once. Keys start with `crucible_`, expire after one year unless a shorter expiry is given, and a user may hold up to ten.
- `GET /account/apikeys` lists keys by name and last four characters; `PATCH` renames one; `DELETE` revokes one.
- Administrators list and revoke a user's keys at `/users/{user_ref}/apikeys`. A key cannot be retrieved after creation.
- A revoked key may remain valid for up to a minute on other server processes.

## Other account routes

| Route | Description |
|---|---|
| `GET /account/identities`, `DELETE /account/identities/{id}` | List linked identities; remove one (never the last) |
| `POST /account/identities/{provider}/link` | Start linking an identity; then `/oauth/link/preview` and `/oauth/link/confirm` |
| `POST /account/password` | Set or change the caller's password |
| `GET /account/auth-events` | The caller's sign-in history |
| `POST /users/{user_ref}/disable`, `/enable` | Administrators disable or enable an account; a disabled account's sessions and keys stop working |

## Deprecated routes

`/auth/hub/login`, `/auth/hub/token`, `/auth/hub/refresh` and `/auth/hub/revoke` provide the same functions as the standard routes with JSON bodies. They are retained for Hub releases before 1.8.0 and will be removed. The browser pages that issued a single API key (`/user_apikey`, `/auth/apikey`) return 410.
