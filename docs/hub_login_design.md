# Hub login design

Status: proposed, not implemented. API branch: `feat/hub-login-design`.

This document records the API-side design for production Hub login. Before changing the Hub contract, coordinate the decision and update `crucible-hub/docs/authentication-transition.md`, its authentication decision, API integration documentation, pinned OpenAPI artifact, generated client, and tests. Do not change the Hub repository as part of this API branch without explicit authorization.

## Current boundary

The Hub is a backend-for-frontend: browser requests reach Hub server functions, which call the API. The browser must hold only an opaque Hub session cookie, never a Crucible API key or ORCID token. Each private Hub server function must authenticate its session; the API must still authorize each request. Hub must send `Crucible-Privilege-Mode: normal` for ordinary operations and request elevation only for explicit administrator operations.

The existing API `GET /auth/login` and `/auth/callback` do not provide a Hub login contract. The ORCID request uses a fixed nonce, does not validate `state`, and the callback does not validate an ID token. `GET /auth/apikey` ultimately exposes a year-long API key through a browser flow. The callback also reads a redirect destination from a cookie without an allowlist. These are separate legacy-flow risks, not capabilities the Hub should inherit. Existing API-key clients must continue to work during migration.

## Recommended first release

Keep the API as the ORCID OAuth client, as the Hub transition document currently assumes, but implement a separate, hardened Hub login flow. Use an established OpenID Connect client library for the ORCID interaction rather than hand-building authorization URLs, token parsing, and ID-token verification. Treat the API-to-Hub handoff as a small first-party one-time exchange, not as an attempt to create a general-purpose OAuth authorization server.

1. Hub creates a login transaction in shared server-side storage, containing random state, PKCE verifier, expected callback, expiry, and a safe relative post-login path. It redirects the browser to an API Hub-login start endpoint with the state and S256 challenge. Do not accept arbitrary redirect URLs.
2. API validates a configured Hub client and exact allowlisted callback, creates its own short-lived ORCID transaction, and redirects to ORCID. Its ORCID transaction has independent random state and nonce, is bound to the browser, and is consumed once. Use ORCID PKCE S256 if confirmed supported in the deployed ORCID environment. Its discovery metadata currently does not advertise `code_challenge_methods_supported`, so do not assume support without an integration test. If unavailable, use the confidential-client code flow with per-transaction OIDC nonce and state binding, as allowed by the OAuth security BCP.
3. On the ORCID callback, API validates its transaction, ORCID issuer, ID-token signature, audience, expiry, nonce, and subject. Reject callback errors, mismatches, expired transactions, and replays without leaking tokens or raw provider responses. Provision or resolve the human API user using the validated ORCID subject. Never derive identity from a caller-supplied ORCID ID or username.
4. API generates a cryptographically random handoff code, stores only its hash with the user ID, Hub client, original Hub state, PKCE challenge, exact callback, and short expiry, and redirects to that callback with only `code` and `state`. Consume the code atomically once. No API credential, ORCID token, or sensitive profile data appears in the URL.
5. Hub validates its original transaction and state, then exchanges the code server-to-server using Hub client authentication and its PKCE verifier. API validates the stored client, callback, expiry, and S256 verifier before issuing a dedicated, user-scoped Hub API bearer credential. Return the user identity, token type, and absolute expiration in a typed response with `Cache-Control: no-store`. Do not return an existing year-long API key or mint a platform credential.
6. Hub stores the credential only in its shared server-side session store, rotates the session ID, and sets a host-only opaque `HttpOnly`, `Secure`, `SameSite=Lax` cookie. Hub makes API calls over HTTPS as the user. The Hub session must not outlive the API credential. For the first release, let expiry require a fresh login rather than introduce refresh-token rotation before it is needed.
7. Logout invalidates the Hub session and revokes the dedicated API credential. Account disablement and credential revocation must take effect across API replicas within a documented bound. A Hub token must use the same per-resource authorization checks as an ordinary human API key, including current ACL and privilege-mode rules.

The dedicated Hub credential should be an opaque random bearer token, stored hashed at rest, with user, client, issued/expiry/revocation timestamps, and no service-account authority. Use a separate token kind or table so legacy API-key rotation cannot unexpectedly replace it, and do not apply the existing 60-second positive API-key cache without an explicit revocation policy. These additions require an Alembic revision and a single linear head. Token lifetime, session lifetime, and acceptable revocation delay require an explicit joint API/Hub operations decision before implementation.

## API contract to publish

- `GET /auth/hub/login`: accepts a registered Hub client identifier, exact registered callback identifier or URI, Hub state, and S256 code challenge; redirects only to the configured ORCID issuer. The exact public URL and input encoding need review against the deployment proxy and base path.
- `GET /auth/hub/callback`: ORCID callback, never a Hub-facing token endpoint. Its success redirects only to a registered Hub callback; errors use a safe generic result.
- `POST /auth/hub/token`: confidential Hub server only. Accepts one-time code, client authentication, registered callback, and PKCE verifier. Returns a typed token response or a generic invalid-grant error. Rate-limit and audit without logging secrets.
- `POST /auth/hub/revoke`: confidential Hub server only. Revokes the supplied Hub API credential; repeated revocation is safe. The exact authentication and response semantics must be specified before coding.

Document redirects and error outcomes in OpenAPI even where a generated client will handle them as browser navigation. Do not put a client secret, bearer token, ORCID token, or code verifier in logs, tracing, errors, analytics, or URLs. Authorization codes necessarily appear on the registered ORCID and Hub callback URLs; redact them from logs and remove them from browser history after processing. Apply `Referrer-Policy: no-referrer` and `Cache-Control: no-store` on callback and token-bearing responses. Use short, bounded outbound ORCID timeouts and reject unexpected provider redirects.

## Alternatives and non-goals

Hub as the ORCID confidential client is also an established backend-for-frontend pattern and removes the API-to-Hub browser handoff. It would still need an authenticated API exchange of the validated ORCID identity for user-scoped API authority, so it does not eliminate the cross-service trust contract. It also changes the Hub's current API-owned-login decision and requires separate ORCID client registration and user-provisioning ownership. Reconsider it if the team wants Hub to own identity-provider integration long term, but do not switch architectures implicitly.

Using the existing year-long API key in a server-side Hub session would keep it out of JavaScript, but a Hub-session leak would grant a much longer-lived credential, and logout would have to rotate a key that other clients may use. Do not use it for production Hub login. A full OAuth authorization server, refresh tokens, browser-held access tokens, and a standing administrator credential in Hub are unnecessary for the first release.

The legacy `/auth/*` and deprecated login aliases should be hardened independently and retained for existing clients until a communicated deprecation. Do not silently redirect old clients into the new Hub flow or alter API-key behavior as part of this feature.

## Verification and rollout gate

Before implementation, confirm ORCID S256 support against sandbox and production configuration, the registered redirect URLs, API and Hub deployment origins, trusted proxy headers, shared transaction/session stores, secret rotation, token/session duration, and account-disable policy. Write a repository-grounded threat model before production use.

Tests must cover state, nonce, issuer, audience, signature, callback, client authentication, PKCE, expiry, atomic one-use exchange under concurrency, replay, code-injection/login-CSRF, safe redirects, revocation across replicas, logout, account disablement, no browser-visible credential, two-account isolation, ordinary ACL enforcement, and administrator elevation. Confirm existing API-key clients and legacy routes remain compatible. Deploy to staging, pin the checked-in OpenAPI contract for Hub, and collect staging evidence before enabling production login.

## Primary references

- [OAuth 2.0 Security Best Current Practice, RFC 9700](https://www.rfc-editor.org/rfc/rfc9700.html)
- [OAuth 2.0 for Browser-Based Applications, RFC 10017](https://www.rfc-editor.org/rfc/rfc10017.html)
- [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0-18.html)
- [ORCID OpenID Connect discovery metadata](https://orcid.org/.well-known/openid-configuration)
- [Authlib FastAPI OAuth client documentation](https://docs.authlib.org/en/v1.6.8/client/fastapi.html)
- [OWASP Session Management Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)
- [OWASP Cross-Site Request Forgery Prevention Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)
