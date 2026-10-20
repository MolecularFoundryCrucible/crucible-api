# How Hub authentication works

ORCID proves who you are. The API issues temporary credentials for your user. The Hub keeps those credentials on its server and gives your browser only an opaque session cookie. Existing API keys and clients continue working unchanged.

This describes the implemented API flow, not a claim that production authentication is enabled. See [rollout status](archive/hub_auth_release_rollout.md), [integration details](hub_login_design.md), and [operations](hub_auth_operations.md).

## From sign-in to logout

| Stage | API database table | What happens |
| --- | --- | --- |
| Start sign-in | `hublogintransaction` | Records a browser-bound attempt before redirecting to ORCID. Expires after 10 minutes and can be consumed once. |
| Return from ORCID | `hublogincode` | After validating ORCID identity, creates a one-use handoff code for the Hub. Expires after one minute. |
| Exchange the handoff code | `hubtokenfamily` | Creates the parent record for this login session, linked to your user and the Hub client. Records absolute expiry and revocation. |
| Issue API credentials | `hubaccesstoken` | Records an access token valid for up to 15 minutes. The Hub uses it to call the API as you. |
| Issue renewal credentials | `hubrefreshtoken` | Records a refresh token that lets the Hub obtain fresh credentials without another ORCID login. |

### 1. Start sign-in

The Hub creates its own pending login state and a private proof value called a PKCE verifier. It sends your browser to `GET /auth/hub/login` with a derived challenge, not that verifier.

The API checks the Hub client and exact callback address, creates a `hublogintransaction`, sets a temporary browser-binding cookie, and redirects to ORCID. This table links the returning browser to the sign-in it started. It is not a logged-in session.

### 2. Return from ORCID

ORCID sends the browser to the API's `/auth/hub/callback`. The API consumes the login transaction, exchanges ORCID's code, and validates the signed identity response. It finds or creates the ordinary `user` record using the verified ORCID identity and ensures its identity-group membership. It does not identify users by email or grant administrator permissions.

The API then creates a `hublogincode` and redirects to the Hub's callback with that short-lived code and the original Hub state. This handoff code is different from ORCID's code and cannot itself authorize an ordinary API request.

### 3. Establish the session

The Hub checks its pending login state, then calls `POST /auth/hub/token` server-to-server with the handoff code, its client credential, the exact callback, and its PKCE verifier. The API checks these together and consumes the code in the same database transaction that creates a token family, access token, and refresh token.

Think of `hubtokenfamily` as the parent record for one login. Every access and refresh token issued for that login points to the same family. Separate sign-ins create separate families.

The API stores hashes of access tokens, refresh tokens, and handoff codes, not reusable plaintext values. The Hub stores the actual access and refresh tokens server-side; they do not belong in browser JavaScript, local storage, or URLs. The shared Hub client credential identifies the Hub server, not the human user, and does not independently grant resource access.

### 4. Use the Hub

The Hub calls normal API routes with the access token. The API looks up `hubaccesstoken`, checks its expiry and `hubtokenfamily`, then loads the user and applies current permissions. Ordinary API requests do not create token rows or extend the family's lifetime. Hub sessions default to normal privilege mode when no `Crucible-Privilege-Mode` header is sent; administrators must request elevation explicitly. Creating an API key from a Hub session requires an ORCID sign-in within the last 10 minutes, measured from ORCID's `auth_time`; otherwise the API returns an RFC 9470 step-up challenge and the Hub re-authenticates with `max_age=0`. Hub sessions cannot read existing API keys. See [API-key retrieval and rotation](api_key_lifecycle_followup.md).

### 5. Refresh credentials

Before its access token expires, the Hub calls `POST /auth/hub/refresh`. The API marks the current refresh row as spent, creates a new refresh row and access row under the same family, and updates the family's last-refresh time.

The limits are 30 days from the original login and no gap of seven days or more between refreshes. Normal API reads do not reset this refresh-idle timer. Existing access tokens may remain valid until their individual expiry unless the family is revoked.

Reusing a spent refresh token revokes the whole family. The Hub must therefore coordinate concurrent requests so only one refresh runs per session. A lost refresh response can require a fresh sign-in.

### 6. Log out

The Hub deletes its own session and cookie and calls `POST /auth/hub/revoke`. The API marks the family revoked, so subsequent authentication checks reject all its access and refresh tokens. Already-authorized requests may finish.

Other login families, existing API keys, and the ORCID browser session are unaffected. If API revocation fails, the Hub must still end its local session but must not claim API revocation succeeded. Session revocation is not account suspension.

## Cleanup and storage

Rows are not immediately deleted when used or revoked. Cleanup deletes expired transactions, handoff codes, and access tokens in bounded batches. Refresh rows, including spent tokens, remain until their family's absolute expiry so reuse remains detectable; expired families and their remaining tokens are then removed.

Each login attempt adds a transaction row. Successful login adds a code, family, access, and refresh row. Each successful refresh adds two token rows. Ordinary resource requests add none. An hourly cleanup job keeps expired records from accumulating indefinitely; it is deployed suspended and requires approved activation. See [cleanup operations](hub_auth_operations.md#expired-record-cleanup).

These five tables live in the API database. They are not the Hub's browser-session store. The single-instance pilot uses `HUB_AUTH_SESSION_MODE=single-instance` with process-local Hub sessions. Restarting or replacing that Hub instance can require another sign-in even while API token records remain valid. Shared Hub session storage is deferred until multi-instance operation is needed.

Disabling `HUB_AUTH_ENABLED` stops login, exchange, refresh, and revoke operations; it does not revoke existing access tokens. Cleanup also does not revoke live sessions. Use the appropriate token-family revocation procedure when ending API access is required.

## Extending this to other clients

Yes, the design can be reused, especially by another trusted web application with a backend, including a Python web app. However, the current implementation supports only one configured client ID and shared client secret per API deployment, with a short list of exact callbacks. It is not yet a multi-client login service or a general OAuth authorization server. Changing those settings would replace the Hub's registration, not add another client.

| Client type | Current support and extension path |
| --- | --- |
| Another first-party web app with a backend | Can reuse the same flow after adding separate client registrations. Its server keeps credentials and performs exchange, refresh, and revocation. |
| Python script, CLI, desktop, or mobile app | Existing API keys still work. Interactive login needs a separately designed public-client flow; do not distribute the Hub client secret. |
| Browser-only application | Cannot safely use the current confidential-client exchange directly. Use a backend like the Hub, or separately design public-client support. |
| Instrument, scheduled job, or other unattended service | Continue using service-account API keys and existing permissions. Human ORCID login and its expiring session are not a replacement for machine credentials. |

### Smallest extension: another trusted backend

1. Add an operator-managed client registry containing each application's unique client ID, its own secret reference, exact allowed callbacks, and enabled state. A small configuration-backed registry can be sufficient initially; a database registration table is optional, not inherently required.
2. Resolve the selected registration consistently at login, code exchange, refresh, and revocation. Keep the existing Hub registration and routes working. Never share the Hub credential between applications or accept arbitrary callbacks.
3. Reuse the existing five tables. `hublogintransaction`, `hublogincode`, and `hubtokenfamily` already store `client_id`; access and refresh rows point to the family. The current schema can represent separate clients without duplicating these tables. Their `hub` names do not require an immediate rename or migration.
4. Give the new backend its own browser-session handling, state validation, PKCE verifier, secure cookies, CSRF protection, refresh coordination, and logout integration. The API remains the ORCID client, so another application's callback is registered with the API, not used as a replacement for the API's ORCID callback.
5. Test cross-client isolation: one application's secret must not exchange, refresh, or revoke another application's grants. Define whether disabling a registration also revokes its existing families; rejecting new exchanges alone does not invalidate issued access tokens. Publish the additive contract and run existing Hub and API-key regression tests.

The application receives the signed-in user's effective API permissions, not a reduced application-specific permission set. Current access tokens are bearer credentials, not sender-bound credentials: possession permits API use without the client secret until expiry or revocation. Client binding protects exchange, refresh, and revocation, but is not a substitute for delegated scopes or consent. Supporting untrusted third-party applications would require a separate authorization design and review.

### Interactive Python and native clients

A distributed CLI or desktop app cannot keep a shared client secret confidential. A future interactive client should use the system browser and authorization code with PKCE, with an appropriate registered callback such as a loopback listener. This requires explicit public-client support in the API; merely removing the existing Basic-auth check is not sufficient. See [OAuth for Native Apps, RFC 8252](https://www.rfc-editor.org/rfc/rfc8252).

For a remote terminal without a usable local callback, evaluate the [Device Authorization Grant, RFC 8628](https://www.rfc-editor.org/rfc/rfc8628). Neither public-client nor device authorization is implemented here, and no ORCID device-flow support is assumed. Prefer an established authorization-server implementation if requirements grow to these standard grants, rather than expanding the custom Hub handoff into one.

A future Python client would coordinate refresh, keep refresh credentials in suitable protected local storage, and send access tokens as bearer credentials. Existing API-key support must remain available. These extensions are future work, not prerequisites for the Hub release.
