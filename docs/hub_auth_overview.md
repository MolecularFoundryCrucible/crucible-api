# How Hub authentication works

ORCID proves who you are. The API issues temporary credentials for your user. The Hub keeps those credentials on its server and gives your browser only an opaque session cookie. Existing API keys and clients continue working unchanged.

This describes the implemented API flow, not a claim that production authentication is enabled. See [rollout status](hub_auth_release_rollout.md), [integration details](hub_login_design.md), and [operations](hub_auth_operations.md).

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

The Hub calls normal API routes with the access token. The API looks up `hubaccesstoken`, checks its expiry and `hubtokenfamily`, then loads the user and applies current permissions. Ordinary API requests do not create token rows or extend the family's lifetime. The Hub requests normal privilege mode for ordinary operations; sign-in does not itself elevate privileges.

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
