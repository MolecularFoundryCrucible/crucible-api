# Hub authentication security review

Date: 2026-09-28. Reviewed API feature revision `8606185`, related Nginx feature configuration, and release handoffs.

## Executive summary

Do not enable public Hub authentication yet. The new authentication flow has appropriate core controls, but the ingress path remains unprotected against direct-backend bypass and uses an HTTP upstream. Review also identified a way for a Hub credential to retrieve a longer-lived API key and an existing HTML-injection issue in the legacy authentication page. These need explicit resolution, not a blanket security assurance.

Scope: API-owned Hub login, token lifecycle, secret loading, authorization integration, cleanup, deployment configuration, and adjacent legacy authentication routes. This is not a complete Hub frontend, cloud penetration test, or dependency audit. Hub CSRF, CSP, browser sessions, and Cloud Run isolation still require the Hub agent's review and deployed verification. No exploit traffic, production writes, or implementation fixes were performed.

## High-priority findings

Disposition for findings 1 and 2, recorded 2026-09-28: infrastructure changes are deferred to preserve existing API access and performance. No sufficiently small, verified non-disruptive solution has been established. Private Tailscale testing may continue; public Hub authentication remains disabled pending mitigation or explicit acceptance of the residual risks. This deferral does not mark either finding as fixed. See [the release decision](hub_auth_release_rollout.md#deferred-ingress-changes).

### 1. Public auth can bypass the proposed proxy limits

- Rule: FASTAPI-DOS-001; deployment boundary. Severity: High, production activation blocker.
- Evidence: `app/api/routes/hub_auth.py:89` exposes browser login without an ingress gate; `app/utils/hub_login.py:90` creates a database transaction for each valid start. Rate/body limits exist only in the undeployed Nginx feature (`../crucible-nginx/nginx.conf:86`). Earlier live inspection confirmed a directly reachable main load balancer and no source-range restrictions. The API has no equivalent rate or body-size enforcement for this flow.
- Impact: unauthenticated callers can bypass proxy limits and consume database space and request capacity once login is enabled. Schema bounds do not bound the complete HTTP body before parsing.
- Fix: deploy controls on every reachable auth path, or make only the new auth paths inaccessible through the legacy direct ingress. Preserve existing API client routing. Do not use a public forwarded header as proof of proxy traversal.
- Mitigation/uncertainty: checked-in deployed flags remain disabled. This is an activation risk, not evidence of a current attack. The controlled Tailscale pilot is not the public ingress test.

### 2. Proxy-to-API auth transport is HTTP

- Rule: FASTAPI-AUTH-002, credential transport. Severity: High, production activation blocker pending transport verification.
- Evidence: `../crucible-nginx/nginx.conf:108` and `:138` use `proxy_pass http://...` to the API load-balancer addresses. `/auth/hub/token`, `/refresh`, and `/revoke` carry the shared Basic credential and token material over this leg.
- Impact: an observer or attacker on an unprotected transport path could obtain reusable credentials. Browser HTTPS alone does not secure this separate connection.
- Fix: use verified TLS, preferably authenticated service-to-service transport, or demonstrate and approve equivalent encrypted network protection. Scope infrastructure changes so legacy routes remain compatible. A secret header over the same unprotected connection is not a transport fix.
- Mitigation/uncertainty: no packet interception was attempted. Cloud network-level protection was not established by this review; HTTP configuration alone does not prove every underlying network segment is unencrypted.

### 3. A Hub access token can retrieve an existing long-lived API key

Disposition on 2026-09-28: the user deferred enforcement and retained current behavior. The intended direction is to retire existing-key retrieval and retain rotation. See [API-key lifecycle follow-up](api_key_lifecycle_followup.md), including the unresolved fresh-authentication requirement for rotation. This risk is documented, not remediated.

- Rule: FASTAPI-AUTHZ-001, credential privilege restriction. Severity: High if Hub-token expiry/revocation is intended to bound compromise duration; requires an explicit product decision.
- Evidence: `app/utils/auth_utils.py:234` accepts a Hub token and returns the same principal used by legacy clients. `app/api/routes/account.py:135` accepts that principal and returns `token.token` at line 150 without credential-type restrictions or fresh authentication. Legacy key validity is up to a year (`app/utils/auth_utils.py:97`).
- Impact: someone holding a stolen valid Hub access token can obtain the user's existing valid API key and retain API access after Hub logout or family revocation. No shared Hub client secret is required for this account route. This does not grant another user's permissions, and requires that the account has an API key.
- Fix: decide whether the Hub should expose API keys. If not, deny this operation for Hub credentials while preserving API-key clients. If it should, require a separately authorized fresh-authentication/step-up flow rather than making a session token sufficient. Merely hiding a Hub button does not enforce the boundary.
- Mitigation/uncertainty: this is confirmed by authorization/data-flow inspection, not a live key retrieval. It may be intentionally desired functionality, but the persistence consequence must be accepted and documented before claiming short-lived tokens limit stolen-credential lifetime.

### 4. Legacy API-key HTML embeds unescaped profile names

Remediated in `b35c825`, released to `main` through PR #287 on 2026-09-28: HTML attributes use standard-library escaping; the update URL uses encoded path components; JavaScript reads escaped data attributes instead of interpolated source literals. Regression tests cover every interpolated parameter and preservation of the submission contract. The evidence below records the pre-fix state.

- Rule: FASTAPI-XSS-001. Severity: High, preexisting adjacent issue.
- Evidence: `app/api/routes/auth.py:143` renders `_email_prompt_html`; lines 179 and 183 interpolate `first_name` and `last_name` into quoted HTML attributes without escaping. A local synthetic name containing a closing quote and inert span markup remained unescaped in the returned HTML. No browser script or live account modification was used.
- Impact: a crafted persisted name can inject active markup when this page is rendered for an account missing email. That page also contains the account's API key. API and proposed Hub share a browser origin, so path prefixes alone do not isolate script privileges.
- Fix: apply context-appropriate escaping or autoescaped templates and add regression tests for both names. Avoid altering the legacy API-key route's supported client workflow. Inspect the existing callback's missing state binding (`auth.py:36`, `:41`) and cookie-selected redirect (`:75`) as a related legacy-auth follow-up.
- Mitigation/uncertainty: rendering injection is confirmed. Cross-user exploitation depends on a path that puts a malicious name in an account rendered to a victim; a full cross-user exploit chain was not demonstrated. This is not an OIDC signature bypass in the new Hub route.

## Medium-priority findings and release requirements

### 5. Dependency remediation remains outstanding

Focused assessment completed on 2026-09-28: see [dependency security assessment](dependency_security_assessment.md) for call-path mitigations, advisory fix floors, and separately tested upgrade batches. No dependencies were changed.

- Rule: FASTAPI-SUPPLY-001. Severity: Medium review requirement, individual exploit severity depends on reachable features.
- Evidence: `uv.lock:2144` pins Starlette 0.46.2; `pyproject.toml:12` pins FastAPI 0.115.14. Starlette is within the affected ranges for [Range parsing](https://github.com/Kludex/starlette/security/advisories/GHSA-7f5h-v6xp-fcq8) and [multipart rollover](https://github.com/Kludex/starlette/security/advisories/GHSA-2c2j-9gv5-cj73). No direct FileResponse, StaticFiles, UploadFile, or request.form use was found in `app`, so those specific exploit paths were not established.
- Other checked alerts: AnyIO 4.13.0 is affected by [IDNA TLS hostname handling](https://github.com/advisories/GHSA-82r6-8w77-94w6); the described attack requires internationalized target hostnames and connection redirection. The reviewed ORCID endpoints use fixed ASCII hosts. Legacy python-jose 3.3.0 is affected by [key-format algorithm confusion](https://github.com/advisories/GHSA-6c5p-j8vq-pqhj), but the inspected legacy decoder restricts its algorithm and the new Hub flow uses Authlib/joserfc instead. These are not proof of an exploitable Hub login bypass.
- Fix: perform a compatible dependency update and review remaining runtime advisories, not an indiscriminate major-version upgrade. Test the locked deployment environment. The alert total spans multiple manifests and is not a count of demonstrated vulnerabilities.
- Positive check: joserfc 1.7.5 is newer than the 1.7.3 fix for the [issuer claim type advisory](https://github.com/authlib/joserfc/security/advisories/GHSA-r74j-q665-7rpj). This is not a complete advisory clearance for all packages.

### 6. Cleanup and emergency revocation must be operational before activation

- Rule: FASTAPI-SESS-001, lifecycle operations. Severity: Medium operational requirement.
- Evidence: cleanup CronJobs are intentionally suspended (`k8s-conf/hub-auth-cleanup-main.yaml:9`, staging equivalent). `hub_auth_enabled()` gates login and client operations, but `hub_access_user()` at `app/utils/hub_tokens.py:159` deliberately does not consult it. Disabling login also disables the normal revoke route via `require_hub_client`.
- Impact: enabling login without cleanup accumulates expired rows. Disabling login is not a session kill switch; already issued access tokens can remain usable until expiry unless their families are separately revoked.
- Fix: activate and monitor cleanup after its approved preview, and document/test an operator procedure for selected or global family revocation during an incident. Do not conflate Secret Manager disablement, login disablement, and immediate token revocation. No automatic global revocation is recommended as part of deployment.

## Accepted constraints, not newly discovered vulnerabilities

- Existing project-wide secret access and the shared Kubernetes identity are user-accepted trust boundaries. No per-workload isolation is claimed.
- The single-instance pilot intentionally loses browser sessions on restart/replacement/misrouting. Shared storage is deferred, but secure cookies, CSRF, account isolation, refresh coordination, and safe failure remain required.
- Email is currently non-unique, but the new login identifies users by verified ORCID and does not link accounts by email. Email uniqueness is a separate documented follow-up.
- Default administrator elevation preserves the existing API contract; the Hub must explicitly request normal mode as designed.

## Controls verified in code

- Exact configured callbacks, S256 PKCE, high-entropy state and browser binding; 10-minute login transactions and one-minute handoffs.
- RS256 allowlist and Authlib claim validation for issuer, audience, expiration, and nonce; fixed ORCID endpoints and outbound timeouts.
- Hashed opaque API tokens, 15-minute access lifetime, bounded refresh-family lifetimes, confidential-client binding, and refresh reuse revocation.
- Transactional handoff consumption and issuance; row locks and reloads serialize refresh/revoke. Current resource permissions are loaded for Hub access without legacy positive-auth caching.
- Public-safe auth errors, no-store responses, redacted application logging, pinned secret versions, and no fail-open fallback after managed-secret failures.

The token design is consistent with the relevant principles in [OAuth Security BCP](https://www.rfc-editor.org/rfc/rfc9700.html), but that is not an assertion that this custom Hub handoff implements every OAuth endpoint or requirement.

## Validation and limits

The full local suite passed outside the restricted sandbox: 1,282 passed, 66 skipped, 13 warnings. The sandbox stall was observed at Starlette TestClient startup in AnyIO, before route execution; no authentication lock deadlock was demonstrated. All six PostgreSQL-specific tests also passed against the isolated local `crucible_hub_auth_test` database, covering issuance rollback, concurrent refresh replay, refresh/revoke races, cleanup, and index migration round-trip. The test server was then stopped. No tests wrote to staging or production. Deployed end-to-end checks, cloud transport verification, and the Hub application review are still required.

No code fix, dependency upgrade, cloud change, migration, or external publication was performed for this review. Keep authentication disabled while resolving findings 1 and 2; address findings 3 and 4 explicitly before public activation.
