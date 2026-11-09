# Security audit, November 2026

Four independent reviewers (Fable, high effort) audited the API on 2026-11-07, one area each: authentication, authorization, files and input, deployment and supply chain. No critical findings; two high, eleven medium. Their full reports follow, unchanged. The reproduction tests they cite were local and are not kept.

## Status, decided by the user on 2026-11-09

| Finding | Decision | Where |
| --- | --- | --- |
| Load balancers and nginx routes (found while answering H1) | nginx to use the internal load balancers, then retire the public ones; fix the NetworkPolicy and the runbook; merge crucible-nginx into the deploy repo. For the deploy session, with the user's authorization | deploy repo |
| High: no request body size limit (files F1, deployment H1) | 10 MiB, 20 MiB for thumbnails (source cap 14 MiB), in the API and the bundle's Caddy; the Foundry's nginx via the deploy session | #473 |
| High: legacy GCS upload routes let a contributor overwrite a dataset's files (files F2) | Retired with API v4, at the end of the local deployment plan | |
| Medium: API keys reach the server log (authentication M4) | Fixed | #466 |
| Medium: a contributor can file deletion requests on any dataset or sample (authorization M1) | Known; the contributor role stays for now and will be reworked | |
| Medium: one address locks out everyone behind it (authentication M1) | Fixed: address delay capped at one minute, known browsers skip it | #467 |
| Medium: an API key renews itself, an admin key reads plain keys (authentication M2, M3) | Removed, answering 410; released together with the Hub from `dev` and nano-crucible 4.0.0, not on production before | #471 |
| Medium: ingestion strings reach the queue unvalidated (files F4) | Stopgap merged; the real fix (ingestors registered by the consumer) is deferred, see Ingestion, open issues, in the Phase 3 plan | #468 |
| Medium: four data scripts write without `--commit` (deployment M4) | Removed | #469 |
| Medium: plain HTTP by default, unencrypted backups (deployment M2, M3) | Guide warns and encrypts backups; HTTPS default in Task 15; plain keys leave with key hashing steps 3 and 4 | #470 |
| Medium: deleted GCS content stays and can be re-attached by basename (files F3) | Later, with the cleanup of orphaned files | |
| Medium: containers run as root (deployment M1) | Image runs as uid 10001 with read-only code; the deploy session prepares the GKE securityContext | #472 |



# Security audit 1: authentication, sign-in and credentials

Repository: `/home/roncofaber/software/crucible-api/.claude/worktrees/selfhost`, branch `feat/password-links` (commit 8620638). Read-only review of `app/auth/`, `app/utils/auth_utils.py`, `permissions.py`, `hub_tokens.py`, `hub_login.py`, the auth routes, templates, `app/config.py`, the auth migrations and the cleanup job, checked against RFC 9700 / 6749 / 7636 / 8252 / 8628 / 7009 / 8414 / 9207, RFC 10027, OWASP ASVS 5.0 V6-V8, NIST SP 800-63B-4 / 63C-4 and the OWASP cheat sheets.

Reproductions live in `/tmp/claude-1000/-home-roncofaber-software-crucible-api/0f41fda4-3511-448e-aa7b-b547605f8c40/scratchpad/audit/test_auth_repro.py`. Run from the repo root with:

```
PYTHONPATH=. uv run pytest -p tests.conftest -q --rootdir=. \
  /tmp/claude-1000/-home-roncofaber-software-crucible-api/0f41fda4-3511-448e-aa7b-b547605f8c40/scratchpad/audit/test_auth_repro.py
```

Result at the time of writing: `10 passed`. Each test's name is given with the finding it supports.

Summary: 0 critical, 0 high, 4 medium, 6 low, 3 info. No authentication bypass, token forgery without the server secret, PKCE or redirect weakness, open redirect or cross-account linking was found. The medium findings are about availability (shared-address lockout), the deprecated API-key routes undoing the "credentials need a browser sign-in" rule, and plaintext API keys reaching logs.

---

## 1. Findings

### M1 (medium) Per-address throttle lets anyone behind a shared address lock out everyone behind it, indefinitely

Evidence: `app/auth/throttle.py:113-129` (`_count_address`), `:136-138` (`begin_attempt` refuses before the account is even looked at), `:172-180` (`begin_code_attempt`, used by `app/api/routes/device.py:75` and `app/api/routes/passwords.py:122,163`). `ADDRESS_FREE_FAILURES = 10`, delays double to `LONGEST_DELAY = 15 min`, counter resets only after `ADDRESS_WINDOW = 1 h` without failures. A refused attempt inside a block is not counted, but the first attempt after the block ends is, which doubles the next block.

Scenario: an attacker sharing a campus NAT, VPN exit, or an upstream proxy that does not set `X-Forwarded-For` to the real client (see A5) sends 11 wrong passwords, then one wrong password every 15 minutes. From then on nobody behind that address can sign in with a password, enter a device code, or open or submit a password link; the correct password is refused before being checked (`AttemptDecision(False)` -> `verify_dummy`). The account counters are never touched, so the account page shows nothing wrong. OIDC sign-in is unaffected.

What does not stop it: the success "give back" (`finish_attempt`, `give_back`) decrements the counter by one but never clears `blocked_until`; the account-level device cookie only exempts from the per-account delays, not the per-address block (`begin_attempt` checks the address first). The design doc accepts "many people behind one address can sign in", which holds only while fewer than 10 failures per hour come from that address.

Confidence: confirmed by reproduction (`test_shared_address_lockout_blocks_a_correct_password`, `test_shared_address_block_can_be_kept_alive_indefinitely`).

Suggested fix: cap the per-address block (for example 1 minute) and treat it as a rate limit rather than a lockout, or let a valid device cookie or a correct password bypass the address block and only count the address toward alerting. OWASP's Authentication Cheat Sheet recommends device cookies precisely so that IP throttles do not lock legitimate users out. Also consider a sliding window of attempts per address instead of exponential blocking.

### M2 (medium) An API key can mint its own successor without any sign-in

Evidence: `app/api/routes/account.py:182-191` (`POST /account/apikey/regenerate`) calls `require_recent_authentication`, which returns immediately for API keys (`app/utils/auth_utils.py:680-681`), then `find_or_add_api_access(..., regenerate=True)` (`:102-123`) which issues a fresh 365-day key (`store_key`, `KEY_LIFETIME`). The same exemption applies to `GET /auth/apikey` style regeneration for admins (`users.py:500-510`).

Scenario: a leaked API key (CI log, notebook, shared cluster home) is about to expire or is listed for revocation. The holder calls the regenerate route and receives a new key valid for another year; the old plain key is revoked in the same call so the owner's CLI breaks, which is the only signal. Expiry therefore never bounds a stolen key, and the stated rule in the design ("Create ... POST /account/apikeys: refused with an API key; browser or Hub with a recent sign-in") has a bypass one route away. ASVS 5.0 V6.4/V7.x and NIST 63B-4 expect re-authentication before issuing a new long-lived authenticator.

What does not stop it: `POST /account/apikeys` correctly refuses API keys (`require_recent_hub_session`), but the deprecated route does not use that check. `reject_hub_api_key_read` only blocks Hub sessions from reading, not keys from minting.

Confidence: confirmed by reproduction (`test_api_key_regenerates_its_own_successor`).

Suggested fix: make `POST /account/apikey/regenerate` and `POST /users/{ref}/apikey[/regenerate]` use `require_recent_hub_session` like `POST /account/apikeys`, or at least refuse when the caller's credential is an API key for the same account. Keep the browser flow (`/auth/apikey/regenerate`) which does have a fresh ORCID sign-in behind it.

### M3 (medium) Admin API keys, including an admin service account's key, can read any non-admin user's plaintext API key and take over their password

Evidence: `app/api/routes/users.py:469-497` (`POST /users/{user_ref}/apikey` returns the stored plaintext `apitoken.token` for API-key callers; only Hub sessions get 409), `users.py:500-510` (regenerate), `app/api/routes/passwords.py:44-65` (`issue_password_link`: `require_platform_admin` + `require_recent_authentication`, exempt for API keys, + `require_human_admin_safeguard`, which returns early for non-admin targets at `auth_utils.py:699`). `app/api/routes/service_accounts.py:96-117` lets a service account be given `platform_role=admin`.

Scenario: a leaked platform-admin API key, or the key of a service account that was made admin, impersonates any non-admin human: fetch their existing plaintext key and act as them (audit trail attributes the actions to the victim), or issue them a password link and set their password, which also ends all their sessions. None of this needs a browser or a recent sign-in. The design table consciously allows "Issue a setup or reset link ... admin key works, and is logged" but the plaintext-key read is the part that turns an admin key into every user's key.

What does not stop it: `require_human_admin_safeguard` protects only human administrators; `reject_hub_api_key_read` only applies to session callers; the `Deprecation` header does not disable the route. The hashed `api_key` table exists, but the plain `apitoken` table is still written on every key creation (`auth_utils.add_apikey`) and read back here.

Confidence: confirmed by reproduction (`test_admin_api_key_reads_another_persons_plaintext_key`); the password-link path follows from code reading (likely).

Suggested fix: stop returning existing plaintext keys to anyone (answer 409 `api_key_exists` for API-key callers too, as already done for sessions); require `require_recent_hub_session` for issuing password links and creating or rotating service accounts, or at minimum refuse these for service-account callers; finish the planned drop of `apitoken.token`.

### M4 (medium) Plaintext API keys reach the server log through SQLAlchemy error text

Evidence: `app/services/activity.py:49-50` runs `update(ApiToken).where(ApiToken.token == api_key)` and the surrounding `except Exception: logger.warning(..., exc_info=True)` (`:52-53`). `app/utils/sql_utils.py:25-33` creates the engine without `hide_parameters=True`, so `str(OperationalError)` contains `[parameters: (..., 'crucible_...')]`. The same pattern (`ApiToken.token == presented`) runs un-caught in `app/auth/api_keys.py:420` (`_adopt_plain_key`), where any database error becomes a 500 whose traceback the framework logs.

Scenario: a transient database fault (failover, pool exhaustion, a lock timeout) during normal traffic writes every active caller's bearer key into the logs in clear, where it is retained and often shipped to a central log system that has a wider audience than the database.

What does not stop it: `record_event` deliberately allows no free text, request logging omits query strings (`main.py:34`), but neither covers exception text. The hashed table is used for lookups, yet the plain table is still updated by token value on every request.

Confidence: confirmed by reproduction (`test_api_key_lands_in_logs_when_activity_update_fails`; the captured log shows the key in `[parameters: ...]`).

Suggested fix: `create_engine(..., hide_parameters=True)`; stop filtering `apitoken` by the token value (filter by `user_id` plus the hash of the token, or drop the plain update entirely); drop `exc_info=True` in `record_caller_activity` or log only the exception class.

### L1 (low) `CRUCIBLE_SECRET_KEY` has no minimum strength although it signs device approvals and other authentication artifacts

Evidence: `app/config.py:637-640` enforces 32+ printable ASCII only for `HUB_AUTH_CLIENT_SECRET`, `OAUTH_*`, and pepper secrets; `SecuritySettings.problems` (`:196-197`) only checks presence. The key signs: device-approval cookie, state and decision tokens (`app/api/routes/device.py:35-36`), the password-link cookie and form token (`passwords.py:68-69`), the throttle device cookie (`throttle.py:87-88`), the keyed hash of device user codes (`devices.py:48-49`) and addresses, the legacy ORCID session cookie and the `crucible_user_token` HS256 JWT (`browser_login.py:37-46`, `auth_utils.py:144-154`).

Scenario: a self-hoster who types a short key by hand instead of following the README's `openssl rand -hex 32`. With the key known or guessed, a decision token `{"d": device_hash, "u": <admin id>, "t": now, "n": nonce}` plus a matching cookie approves an attacker's device as any user (reproduced with a known key), and the legacy JWT yields any ORCID holder's API key.

What does not stop it: README generates a strong key; `settings check` does not validate length; Secret Manager path does not either.

Confidence: confirmed by reproduction of both the acceptance (`test_one_character_secret_key_is_accepted`) and the forgery given the key (`test_forged_device_decision_with_known_secret`); exploitability depends on operator error (possible).

Suggested fix: add `CRUCIBLE_SECRET_KEY` to the minimum-length rule (32 chars), and reject it in `check_settings`. Consider deriving per-purpose keys with HKDF so one leaked artifact's key does not sign the others.

### L2 (low) Admin service accounts are elevated by default while explicit elevation is refused

Evidence: `app/utils/permissions.py:171-178`: with no `Crucible-Privilege-Mode` header, any platform admin with a non-session credential gets `ELEVATED`, including a service account; `:167-169` refuses the same service account when it asks for `ELEVATED` explicitly, and `can_elevate` is `False`. `service_accounts.py:96-117` allows `platform_role=admin` on service accounts.

Scenario: an operator makes an automation account admin for a bounded job; its key then carries full elevated authority on every request (creating service accounts, issuing password links for non-admins, revoking keys, ending sessions), with `can_elevate=false` reported to clients. The inconsistency makes the privilege model hard to reason about and means the "service accounts cannot elevate" rule is only enforced on the explicit path.

Confidence: confirmed by reproduction (`test_admin_service_account_is_elevated_by_default_but_cannot_ask_for_it`).

Suggested fix: either forbid `admin` for service accounts (schema `Literal["none", "contributor"]`), or make the compatibility default honour `can_elevate` so service accounts fall to `NORMAL` unless explicitly allowed.

### L3 (low) Refresh-token reuse grace lets a thief shadow the owner without detection

Evidence: `app/utils/hub_tokens.py:140-147` (`_reusable`: a spent token of generation `newest-1` is accepted for 30 s) and `:184-200`: a reused token mints a new generation rather than returning the already-issued successor, and spending any token of a generation marks its siblings spent, which restarts the 30 s grace for them.

Scenario: a copied refresh token (public client on a shared cluster, CLI config file). Access tokens last 15 minutes, so clients refresh on a predictable cadence. The thief refreshes a few seconds before each expected owner refresh: the owner's token has just been marked spent, is within grace and `newest-1`, so it is accepted, and both parties receive fresh tokens every round. Neither replay detection nor family revocation ever fires. RFC 9700 4.14.2 requires reuse to be detected; a small grace is common practice, but here it is renewable.

Confidence: confirmed by reproduction (`test_refresh_grace_lets_a_thief_shadow_the_owner`: six rounds, family not revoked, two live refresh tokens).

Suggested fix: on reuse within grace, return the same successor that was issued the first time (idempotent retry) instead of minting another, and do not restart grace when siblings are marked spent; alternatively allow at most one grace reuse per family.

### L4 (low) `/oauth/authorize` without `redirect_uri` answers with a redirect to the literal address `None?...`

Evidence: `app/api/routes/hub_auth.py:207-219`: Authlib accepts the omitted parameter because `OAuthClient.get_default_redirect_uri` returns the first registered URI (`oauth_server.py:104-105`), then `check_login_request` refuses `None` and `_hub_error(redirect_uri=None, ...)` builds `None?error=invalid_request&state=...&iss=...` (`hub_login.py:239-250`).

Scenario: not an open redirect (the target is relative, `/oauth/None`), but the error path is inconsistent with RFC 6749 4.1.2.1 (a missing or invalid redirect must be answered directly, never redirected), and it shows the two validation layers disagree.

Confidence: confirmed by reproduction (`test_authorize_without_redirect_uri_redirects_to_none`, Location `None?error=invalid_request&...`).

Suggested fix: require `redirect_uri` explicitly (return 400 when absent) before calling Authlib, or make `get_default_redirect_uri` return `None` so Authlib raises the direct error.

### L5 (low) `POST /oauth/device` accepts cross-site form posts, which lets a phisher skip the code-entry warning page

Evidence: `app/api/routes/device.py:72-93`: no CSRF token, origin or `Sec-Fetch-Site` check; the response sets the approval cookie (SameSite=Lax is fine for the following top-level navigations) and redirects into sign-in. `templates/device_code.html:19` holds the "never enter a code someone sent you" warning that this skips.

Scenario (RFC 8628 5.4, RFC 10027): the attacker starts a device flow, then has the victim's browser auto-submit `user_code` to `/oauth/device`. The victim lands directly on the provider sign-in and then on the approve page. The approve page still names the client, the person and the requesting address and still needs a click, so this is an accelerator rather than a bypass.

Confidence: likely (by reading; the request path has no state the attacker lacks).

Suggested fix: refuse `POST /oauth/device` unless `Sec-Fetch-Site` is `same-origin`/`none` (or check `Origin` against the public API URL), and consider not honouring `verification_uri_complete` auto-fill without a confirmation click.

### L6 (low) Legacy `/auth/apikey` page embeds the API key in the DOM, loads third-party scripts without CSP or SRI, and uses a cookie with no SameSite attribute

Evidence: `app/api/routes/auth.py:122-227` (`_email_prompt_html`: `data-api-key="{crucible_token}"`, `<script src="https://cdn.jsdelivr.net/...">`, Google Fonts, no CSP, no X-Frame-Options), `auth.py:74` (`crucible_user_token` cookie: `httponly`, `secure`, no `samesite`), `auth.py:230-246` (`POST /auth/apikey/regenerate` authenticated by that cookie alone).

Scenario: a compromised or misbehaving CDN script reads the key from the page; in browsers that do not default cookies to Lax (Firefox, Safari), a cross-site POST within the cookie's 60 s lifetime regenerates the victim's key (CSRF causing a key rotation they did not ask for). The other sign-in pages all carry a strict nonce CSP, so this page is the outlier.

Confidence: likely (by reading).

Suggested fix: serve this page from the Jinja templates with the same CSP (`default-src 'none'; script-src 'nonce-...'; style-src 'nonce-...'; frame-ancestors 'none'`), inline the small script and style instead of the CDN, set `samesite="lax"` on both legacy cookies, and show the key only after an explicit click.

### I1 (info) Legacy ORCID browser flow registers anyone when `CRUCIBLE_AUTH_PROVIDERS` is unset

Evidence: `app/api/routes/auth.py:78-82`: `registration = orcid_provider.registration if orcid_provider is not None else "open"`. Only reachable when the legacy `ORCID_CLIENT_ID` settings are configured; the Foundry relies on this. A self-hoster who configures ORCID through the legacy settings rather than the providers list gets open registration without having chosen it.

Suggested fix: default to `closed` (or `approval`) when no provider entry exists, and say so in the self-hosting guide.

### I2 (info) Unauthenticated endpoints write rows without any rate limit, and `/oauth/*` lacks the body cap Caddy applies to `/auth/hub/*`

Evidence: `GET /oauth/authorize` inserts a `HubLoginTransaction` per request (`hub_login.py:117-138`); `POST /oauth/device_authorization` inserts a `DeviceAuthorization` for any public `client_id` (`devices.py:57-70`); `deploy/compose/Caddyfile:14-23` caps only `/api/v3/auth/hub/*` at 8 KB and 20 s. Form bodies to `/oauth/password` and `/oauth/password-link` are read fully by Starlette before the `max_length` validators run.

Suggested fix: extend the Caddy `request_body` limit to `/api/v3/oauth/*`; add a per-address rate limit on the two row-creating routes (the throttle table could be reused).

### I3 (info) Positive auth cache keeps a revoked or disabled key valid for up to 60 s on other workers

Evidence: `app/utils/auth_utils.py:41-47`, `:224-244`; `AUTH_CACHE_TTL` read from the raw environment, not from settings. Documented in code and in the disable route's docstring; `forget_cached_keys` clears only the current process. Acceptable, listed for completeness. A shared cache (or a per-user "credentials changed at" check) would close the window.

---

## 2. Areas of improvement (not vulnerabilities)

- Proxy trust. `FORWARDED_ALLOW_IPS="*"` with uvicorn 0.54 takes the leftmost `X-Forwarded-For` entry. The bundle is safe because Caddy 2.5+ replaces client-supplied forwarding headers, and the README says the API must not be reachable except through the proxy. Add a startup warning when `FORWARDED_ALLOW_IPS=*` is set and the bound address is not loopback or a container network, and document that any proxy placed in front of Caddy must be listed in Caddy's `trusted_proxies` or every user shares one address (which, with M1, locks everyone out together). The `requested_from` shown on the device approval page inherits the same trust.
- Self-service password change. The design describes `POST /account/password` (current password plus recent sign-in); it is not implemented, so a person cannot change or set their own password without an administrator link. `session_authenticated` in `begin_attempt` has no caller yet.
- Password-link lifetime. Default 24 h, up to 168 h (`password_link_hours`). OWASP's Forgot Password cheat sheet recommends much shorter validity for reset tokens (minutes to a few hours); consider a shorter default for `reset` than for `setup`.
- Notifications. No in-app or mail notice when a login is linked or unlinked, a password is set, a key is created, or a device is approved (NIST 63C-4 3.8, 63B-4 5.2.x). The `auth_event` table has what is needed; surfacing it in the Hub account page would help.
- `_count_address` sets `blocked_until` in a second statement after the upsert, so two simultaneous failures can leave the shorter block; a single `UPDATE ... RETURNING` with a `CASE` on the new count would be atomic.
- `find_or_add_api_access` uses `.one_or_none()` on `apitoken` rows created within a year; two rows (possible for legacy data) raise `MultipleResultsFound` (500).
- `_password_holder` matches usernames with `lower()` and `.first()`; the unique index is case-sensitive, so legacy data differing only in case would pick an arbitrary account. `assert_username_available` prevents new collisions, so this is only about old rows.
- Device-code entry (`POST /oauth/device`) and link opening count against the address limit only; add a global ceiling on pending device authorizations per client to bound table growth and keep the 34-bit code space sparse.
- Password links count the first open as a failure and then give it back; a mail scanner that follows a link from a shared egress address therefore nudges that address toward M1. Count only wrong tokens.
- Tests to add: a shared-address scenario that expects a correct password to still work; `POST /account/apikey/regenerate` with an API key; `POST /users/{ref}/apikey` with an admin API key when a plaintext key exists; `hide_parameters` on the engine; refresh reuse with two alternating holders; `/oauth/authorize` without `redirect_uri` answering 400.
- `hub_access_family` does not check the idle lifetime (`last_refreshed_at + idle`), only `expires_at`; harmless because access tokens live 15 minutes, worth a comment.
- `SessionMiddleware` for the legacy ORCID flow is `https_only=True` while the new pages compute `secure` from the public URL; align the two (the legacy flow silently fails on `http://localhost`).

---

## 3. Examined and found sound

- Authorization code grant: PKCE S256 required (`PkceS256(required=True)`), verifier length and charset checked, challenge compared with `compare_digest` inside the same `SELECT ... FOR UPDATE` that consumes the code (`hub_login.py:271-283`); code bound to client, exact redirect URI and purpose; codes hashed; 1 min lifetime (5 min for public clients behind the confirmation page); single use. `redirect_uri` on the token request must equal the one the code was issued for.
- Redirect matching: exact, `https` only except loopback/localhost, no query/fragment/userinfo; loopback any-port only for public clients and only for `127.0.0.1` / `::1` (RFC 8252 7.3). Errors are redirected only after Authlib verified client and redirect; otherwise 400. `iss` is appended to every answer (RFC 9207), including errors.
- Client authentication: `client_secret_basic` for confidential, `none` for public, the internal device-approval client refused at every token-endpoint method and grant; secrets compared with `compare_digest`, and a dummy comparison runs for unknown clients. Public clients cannot use the Hub's JSON routes (Basic only). Secrets must be 32+ printable ASCII, given exactly one way.
- Token and refresh handling: tokens are random (`token_urlsafe(48)`), stored as SHA-256; access 15 min, families bounded by absolute and idle lifetimes; refresh rotation with family revocation on reuse beyond the grace window; refresh and revocation check the family's `client_id`; revocation treats other clients' tokens as unknown (RFC 7009); `Cache-Control: no-store` on all token responses.
- Upstream OIDC: per-provider callback paths plus stored `provider_key` give the RFC 9700 mix-up defence; `iss` response parameter enforced when advertised; discovery issuer must match configuration, `none`/`HS*` algorithms removed; ID token `iss`, `aud` (single audience) and `nonce` enforced (Authlib raises `MissingClaimError` when the nonce claim is absent); PKCE toward the provider when it advertises S256; `auth_time` rejected if in the future; `prompt=login` with `max_age=0` for step-up.
- Browser binding of sign-ins: per-sign-in cookie named by a hash of the state, random browser secret stored hashed, state consumed under row lock, 10 min lifetime, cookies `HttpOnly`, `SameSite=Lax`, `Secure` on https, path-scoped.
- Device grant (RFC 8628, RFC 10027): device code hashed, user code (34 bits) stored as HMAC with the server key, 10 min lifetime, poll interval with `slow_down` escalation, approval consumed by a conditional `UPDATE ... RETURNING` so a code works once, approval needs a sign-in no older than 10 min, approval bound to the browser by signed cookie + nonce, sessions obtained by device code are excluded from every step-up action (`via_device`), approval page shows client, person, time, requesting address and the warning text.
- Public client confirmation: the code is shown behind an Allow/Cancel page for public clients (RFC 8252 8.6), with `Referrer-Policy: no-referrer`.
- Passwords: Argon2id via pwdlib with an OWASP-floor validator on the parameters, HMAC pepper outside the database with rotation (`pepper_id`, previous peppers), NFC normalization, 15-char minimum (12 allowed), 256 max, blocklist, no composition rules, personal-data check (NIST 63B-4 3.1.1.2); one generic failure message and the same hashing work for unknown, disabled, locked and pending accounts; a success is accepted only if `password_changed_at` is unchanged; account counters updated atomically before hashing; concurrency of hashing bounded by a semaphore; passwords on requires an https public URL at startup.
- Password links: token hashed, single live link per person (partial unique index), opening does not consume, token moved from the URL to a path-scoped cookie, form protected by a nonce bound to that cookie, consumption under row locks on token and user, sets password, clears locks, ends all sessions, voids pending sign-in codes and device approvals, never signs the person in; refused for service accounts, disabled and pending accounts; links voided on disable, identity link/unlink and account move.
- Linking: started only from a Hub session with a recent sign-in, confirmed only by the same session family (`session_family_id`), PKCE on the link code, start address single use and valid 2 min, 5 starts per 10 min, provider must have re-authenticated (`auth_time` >= start), identity uniqueness by constraint plus `IdentityTaken` handling, one ORCID per account by partial unique index, last sign-in method cannot be removed, unlink ends other sessions and cancels in-flight links.
- Registration and account state: sign-in resolves by exact issuer+subject only; `closed`/`approval`/`open` per provider; pending accounts receive no code, token or key on any path (`_may_hold_hub_tokens`, API-key check in `_authorize_user`, `check_link_target`); disabled accounts are refused at sign-in, code redemption, refresh, access-token use and API-key use, and disabling revokes all sessions and voids links; enabling does not resurrect sessions. Rejection and expiry of pending accounts refuse when anything references the account.
- Service accounts: can never hold passwords (`ServiceAccountPassword`), sessions (`_may_hold_hub_tokens`), or identities; cannot manage platform roles or switch accounts; effective role capped at contributor; cannot grant ACLs.
- Administrator safeguards: changes to a human administrator, promotions to admin, reading or regenerating another administrator's key need a Hub session with a sign-in within 10 min and never an API key; last-human-admin protection for demotion and disabling under row locks; callers cannot change their own role or switch themselves off.
- Step-up: RFC 9470 challenge with `max_age` and `WWW-Authenticate`; `auth_time` preserved across refreshes.
- Browser pages: nonce-based CSP (`default-src 'none'`), `form-action` restricted, `base-uri 'none'`, `frame-ancestors 'none'` plus `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`; Jinja autoescaping on; the password form is CSRF-safe through the state-bound cookie; sign-in chooser starts nothing.
- Logging and events: request log omits query strings; `record_event` accepts no free text; refusals are logged by class name only; Authlib logs only client ids.
- Settings: each secret given exactly one way, blank counts as missing, min length and single-line checks on client secrets and peppers, `CRUCIBLE_AUTH_CLIENTS` and `HUB_AUTH_ENABLED` cannot both be set, worker refuses to start if a secret in use cannot be read.
- Cleanup job: deletes only expired transactions, codes and access tokens, refresh tokens only with their expired families, device rows and password tokens a day after expiry, throttle rows after 30 idle days, events after the retention setting, pending users via `reject_pending_user` which refuses referenced accounts; uses `SKIP LOCKED` batches.
- Migrations: `apitoken_revoke_trigger` keeps the hashed copy revoked when a plain row is deleted by any writer; `api_key` adoption handles the race with a nested transaction.

# Audit 2: Authorization and data access control on the data routes

Repository: `/home/roncofaber/software/crucible-api/.claude/worktrees/selfhost` (branch `feat/password-links`). Read-only review of `app/utils/permissions.py`, `app/utils/auth_utils.py`, `app/utils/sql_utils.py`, `app/services/resource_*.py`, `app/utils/link_utils.py`, `app/utils/project_utils.py`, `app/utils/user_utils.py`, and every route module listed in scope, against OWASP API Security Top 10 (2023) and ASVS 5.0 V8.

Reproductions live in `/tmp/claude-1000/-home-roncofaber-software-crucible-api/0f41fda4-3511-448e-aa7b-b547605f8c40/scratchpad/audit/test_authz_repro.py` (10 tests, all passing against the in-memory database; each asserts the current behaviour). Run from the repo root with `PYTHONPATH=. uv run pytest <file> -q -p no:cacheprovider --rootdir=.`.

Summary: no critical or high findings. The permission core (MIN/MAX role model, `permission_filter`, `require_permission`, `require_grant`, signed cursors) is sound and consistently applied on dataset, sample, file, link, deletion, join-request and ACL routes. The findings below are mostly places where the platform-wide `contributor` policy or a convenience route is broader than the design documents say, plus a few information-exposure issues.

Counts: critical 0, high 0, medium 1, low 8, info 3.

---

## 1. Findings

### M1 (medium). Any platform contributor (every service account) can file a deletion request on any dataset or sample, hiding it from all lists and freezing writes

Evidence
- `app/api/routes/resources.py:547-563`: for `dataset`/`sample`, `create_deletion_request` requires only `Permission.CREATE`.
- `app/utils/auth_utils.py:318-321` and `app/utils/permissions.py:263-269`: `has_global_permission` grants CREATE to `platform_role=contributor` on every resource; `require_permission` short-circuits on it.
- `app/api/routes/service_accounts.py:158`: every new service account is `platform_role=contributor`.
- `app/utils/sql_utils.py:297-307` (`deletion_filter_for`) and `:244-254` (`assert_not_pending_deletion`): a pending request removes the record from every list, search, facet, graph and link traversal, and returns 409 on file adds, uploads, links, ingestion and postprocessing.

Attack scenario
A service account key (an instrument control PC; these keys live on shared lab machines) or any human given `contributor` platform role calls `POST /resources/{mfid}/delete` for every dataset and sample MFID it can see (it can see all of them: global READ). Every record disappears from `GET /datasets`, `/samples`, search, facets, graphs and from all `links` arrays for every user, and every write to them fails with `pending_deletion`, until an admin on each record individually rejects each request (`PATCH /deletion_requests/{id}` needs `DELETE`, i.e. record admin/owner). Platform contributors cannot reject, so the only unwinder is the owner, project admins or a platform admin. The one-active-request index means the owner cannot pre-empt it either.

What does not stop it
- `docs/permission_system_plan.md` says contributors may request deletion of datasets/samples; that is the intended per-project threshold, but combined with the platform-wide contributor policy it becomes platform-wide.
- There is no rate limit or per-requester cap on deletion requests (join requests have `MAX_PENDING_PER_REQUESTER = 20`; deletion requests have none).
- The record stays readable by exact MFID (`select_readable`), so this is availability/integrity, not confidentiality.

Confidence: confirmed by reproduction (`test_service_account_can_freeze_and_hide_strangers_dataset`).

Suggested fix
- Require a real ACL role for deletion requests: compute `effective_role(principal, acl_rows)` and demand `>= CONTRIBUTOR` from the ACL (not `has_global_permission`), or at minimum exclude `is_service_account` principals from filing requests on records they do not own (the model already does this for join requests, `join_requests.py:296`).
- Add a per-requester pending cap like join requests, and consider not hiding a record from its own owner/admins while a request is merely `pending`.

### L1 (low). A project can be made public through the generic public-access route, exposing its member list to every authenticated user and leaving an undiscoverable grant

Evidence
- `app/api/routes/resources.py:942-963` `publish_resource` calls `require_permission(MANAGE_ACL)` and `require_grant(VIEWER)` but never `_reject_project_grants`, unlike `set_resource_access` (`:880`) and `revoke_resource_access` (`:906`) which refuse every project grant with 409 `use_project_members`.
- `app/api/routes/projects.py:418` `read_users_on_project` gates on `Permission.READ` via the project ACL, so a public ACL row makes the member list readable by everyone.
- `app/api/routes/resources.py:838` `list_resource_access` requires `may_view_members`, so a non-member cannot even see that the public grant exists; `load_project_detail` never shows it either.

Attack scenario: a project admin (standing `admin` or `owner`) runs `PUT /resources/{project_mfid}/access/public`. From then on every authenticated user can `GET /projects/{ref}/users` and read the full roster (names, usernames, roles, ORCIDs). The project appears in `GET /projects?accessible_to_user=...` for everyone. Nothing in the UI/API model represents a "public project", so it is unlikely to be noticed or reversed.

What does not stop it: `with_grant_capabilities` marks project grants as `fixed`, but that only affects capability flags, not the route.

Confidence: confirmed by reproduction (`test_project_can_be_made_public_exposing_member_list`).

Suggested fix: call `_reject_project_grants(resource)` in `publish_resource` and `unpublish_resource` (and consider a one-off audit for existing public rows on project MFIDs).

### L2 (low). Instrument scientific metadata is served without any ACL check through the instrument routes, while the metadata route enforces the instrument ACL

Evidence
- `app/services/resource_details.py:265-310` `load_instrument_detail` has no `require_permission` and returns `scientific_metadata` when `include_metadata=true`; used by `GET /instruments/{ref}` (`instruments.py:292`) and `GET /resources/{mfid}` (`resources.py:353`).
- `app/api/routes/instruments.py:193-233` `list_instruments` joins `ScientificMetadataBinary` and returns it for every row with `include_metadata=true`; `search_instruments` has no ACL filter either.
- By contrast `GET /resources/{mfid}/metadata` (`resources.py:434`) and `GET /resources/metadata/search` (`resources.py:283`) apply `permission_filter`/`require_permission` READ on the same rows and return 403 / exclude them.

Attack scenario: a stranger reads instrument calibration or configuration metadata (whatever operators chose to store there) for every instrument with `GET /instruments?include_metadata=true`, although the same data is denied via `/resources/{mfid}/metadata`.

What does not stop it: instrument records themselves are intentionally readable by all authenticated users (tests in `tests/test_instrument_maintainers.py` expect strangers to get 200), so this is a scope inconsistency rather than a broken gate. The instrument ACL only ever holds owner/maintainers, so the "strict" route is in practice unusable by anyone else.

Confidence: confirmed by reproduction (`test_instrument_metadata_readable_by_anyone_via_instrument_routes`).

Suggested fix: decide one policy. Either treat instrument metadata as public like the rest of the record (and relax `/resources/{mfid}/metadata` for instruments), or gate `include_metadata` on instruments behind `permission_filter`/`record_role` the way `members` is.

### L3 (low). `GET /keywords`, `GET /keywords/{keyword}` and `GET /ingestors` require no authentication

Evidence: `app/api/routes/misc.py:134-159,162` have no `Depends(authorize_user)`; `app/api/main.py:342` includes the router with no dependencies. Already noted as an open question in `docs/routes_audit_2026-10.md:8`.

Attack scenario: an unauthenticated client enumerates every keyword ever attached to any dataset, including private ones, with per-keyword dataset counts (`num_datasets`). Keywords are free text chosen by users and frequently name samples, materials, collaborators or proposals. `/keywords/{keyword}?permissive=true` uses `ilike` on the raw value so `%` wildcards allow prefix probing.

What does not stop it: nothing; there is no global auth middleware (`login_session_middleware` only handles browser sessions).

Confidence: confirmed by reproduction (`test_keywords_require_no_authentication`).

Suggested fix: add `authorize_user` to the three routes (ingestors is harmless but should not be the odd one out), and scope `/keywords` to keywords on datasets the caller can read, or at least stop returning `num_datasets` to non-admins.

### L4 (low). Email address to person oracle for any authenticated user

Evidence
- `app/api/routes/users.py:449-460` `read_user` resolves `user_ref` through `resolve_user_by_identifier`, which accepts an email (`user_utils.py:332`), and returns the public profile (unique_id, username, ORCID, name) with 200, or 404.
- `users.py:190,219-220` `GET /users?email=` exact lookup is explicitly exempt from the shared-group scoping for non-admins.
- `users.py:331-370` deprecated `POST /users/resolve` accepts a batch of emails.
- Also usable indirectly: `owner_id`/`owner` filters on `/datasets`, `/samples`, `/instruments` (`user_utils.owned_by`), `project_lead_email` on `POST /projects`, `email=` on project member routes, `PUT /resources/{mfid}/access/users/{email}`.

Attack scenario: a low-privilege account (or any lab self-hosting with open registration) confirms whether a given email belongs to a registered person and learns their name, username and ORCID in bulk (`/users/resolve` has no size limit on `emails`). This is PII disclosure (ASVS V8.3) and account enumeration.

What does not stop it: `UserPublicRead` omits the email itself, and docs say exact filters are intentionally global; but mapping email to identity is the sensitive direction.

Confidence: confirmed by reproduction (`test_email_to_user_oracle_for_non_admins`).

Suggested fix: restrict email-based lookup to platform admins (and to project editors adding members, where the use case lives), remove `emails` from `/users/resolve` for non-admins, and cap batch sizes.

### L5 (low). Any authenticated user can create a project and seat an arbitrary other person as its owner

Evidence: `app/api/routes/projects.py:79-143` `create_project` has no authorization beyond login (service accounts included) and resolves `project_lead` to any existing user; the creator receives no membership at all.

Attack scenario: a user mass-creates projects led by a victim (they appear in the victim's `GET /users/{id}/projects`, in `/projects/search` for everyone with the victim's ORCID as lead, and consume the `project_id` namespace), or creates a project owned by a service account that can never administer it (`_validate_service_account_project_role` is not applied to the lead). The design doc lists `POST /projects` authorization as an open decision (`docs/permission_system_plan.md:780`).

Confidence: confirmed by reproduction (`test_anyone_can_create_project_owned_by_someone_else`).

Suggested fix: require the lead to be the caller unless the caller is a platform admin (same rule as `POST /instruments`, `instruments.py:342-349`), reject service accounts as lead, and consider a registrar role for self-hosted labs.

### L6 (low). Client-chosen dataset/sample MFIDs can collide with a user's identity (service-account `unique_id` / identity group name)

Evidence: `app/utils/sql_utils.py:66-85` `assert_mfid_available` checks only `Resource.mfid`; `create_service_account` (`service_accounts.py:139-146`) checks `Resource` and `AccessGroup` in the other direction, so the invariant is enforced only one way. `POST /datasets` and `POST /samples` accept `unique_id`.

Attack scenario: a contributor creates a dataset whose `unique_id` equals an existing service account's MFID. Afterwards `/users/{mfid}` and `/resources/{mfid}` name different things, the dataset's ACL rows have `mfid == AccessGroup.group_name` of the SA's identity group (the join used by `_review_authority`, `join_requests.py:99`), and user/resource resolvers that sniff by format (`resolve_user_by_identifier`, `_canonical_user_id`) become ambiguous. I found no direct privilege escalation, but the namespace assumption "an MFID names exactly one thing" is relied on in several places.

Confidence: confirmed by reproduction (`test_dataset_mfid_can_collide_with_service_account_identity`); impact is possible, not demonstrated.

Suggested fix: make `assert_mfid_available` also refuse MFIDs present in `User.unique_id` and `AccessGroup.group_name`.

### L7 (low). Service accounts cannot publish, but can create records that are public from the start and owned by someone else

Evidence: `app/api/routes/datasets.py:244` and `samples.py:276` call `set_public(..., bool(ds.public))` from the request body with no `MANAGE_ACL`/`require_grant` check, while `publish_resource` (`resources.py:955`) refuses service accounts via `require_grant`. `DatasetCreate.public` / `SampleCreate.public` default False but are accepted.

Attack scenario: a service account (contributor ceiling, explicitly barred from ACL management) creates a dataset in a confidential project with `public: true` and `owner: <researcher>`; the record is world-readable immediately and the researcher is listed as its owner. The design doc's claim that publishing "already requires admin as a consequence of the model" (`permission_system_plan.md:605`) does not hold on the create path.

Confidence: confirmed by reproduction (`test_service_account_publishes_at_creation_despite_publish_ban`).

Suggested fix: on create, only honour `public=true` when the caller would pass `require_grant(VIEWER)` on the new record (owner of own record or platform admin); otherwise 403 or ignore with a warning.

### L8 (low). Group standing changes propagate to API-key callers only after the auth cache TTL; project removal and demotion never invalidate it

Evidence: `app/utils/auth_utils.py:41-47,224-244` caches the whole `Principal` (including `groups` and `platform_role`) per API key for `AUTH_CACHE_TTL` (60 s default) per worker. `forget_cached_keys` is called only from `users.py:693,771,809` (platform role change, disable, key revoke). `remove_user_from_project`, `set_project_user_role`, `remove_instrument_service_account` and `remove_user_from_access_group` do not call it. Hub sessions are not cached (`:212-222`).

Attack scenario: a member removed from a project (or demoted from admin to viewer) keeps their old standing on every worker for up to a minute and can still read, edit or re-grant within that window. ACL row changes are live, so this only affects group standing.

Confidence: likely (code reading; not reproduced because the test fixtures bypass `authenticate_user`).

Suggested fix: call `forget_cached_keys(session, user.id)` wherever a `UserAccessGroupLink` is changed or deleted, or key the cache entry by a per-user version that membership changes bump.

### I1 (info). Project membership management allows peers to manage peers, contrary to the design document

Evidence: `projects.py:484-490,548-555,600` and `join_requests.py:435` call `require_grant(..., floor=Role.EDITOR)` with the default `allow_peer=True`; `permissions.can_grant` therefore lets an editor seat, demote or remove another editor and an admin another admin. `docs/permission_system_plan.md:341` says "A role cannot create, promote, demote, or otherwise modify a peer." The route docstrings describe the implemented (peer-allowed) behaviour.

Confidence: confirmed by reproduction (`test_editor_can_seat_and_demote_another_editor`). Not an escalation above one's own role; reported because two editors can lock each other out and the documentation promises otherwise.

Suggested fix: pass `allow_peer=False` on the project membership routes, or update the design doc.

### I2 (info). Private record existence oracle (403 vs 404) and sequential ids

Evidence: `require_permission` is called after the existence lookup in most singleton routes (`load_dataset_detail`, `load_sample_detail`, `get_resource_links`, `read_resource_metadata`, `generate_dataset_download_links`, `read_associated_files`), so a private MFID yields 403 and a missing one 404. `get_deletion_request` (`deletion.py:513-521`) does the same for sequential integer ids, which also leaks the total number of requests. MFIDs are 26 random Crockford characters, so enumeration is impractical; this confirms rather than discovers.

Confidence: confirmed by reproduction (`test_private_record_existence_oracle`).

Suggested fix: return 404 for both cases on singleton reads, as `select_readable`-based routes (files, ingestion requests, postprocessing runs) already do.

### I3 (info). Privilege mode defaults: admin API keys are always elevated

Evidence: `app/utils/permissions.py:158-171` `resolve_authorization_context` gives `COMPATIBILITY_DEFAULT` elevated mode to any platform admin whose credential is not a Hub session when the header is omitted, including administrator service accounts that cannot request elevation explicitly. `docs/explicit_privilege_mode_design.md:31` labels this temporary. The effect is that a leaked admin API key bypasses every ACL with no extra signal, and admins using API clients get no least-privilege by default, whereas the same person in the Hub does.

Confidence: confirmed by code reading and the existing tests of `resolve_authorization_context`.

Suggested fix: flip the default to NORMAL for API keys on a dated schedule (nano-crucible can send the header), and remove the compatibility default for service accounts first.

---

## 2. Areas of improvement (not vulnerabilities)

- Platform-wide contributor policy for service accounts. Beyond M1, a service account can on any record it did not create: write initial metadata (`resources.py:455`), add keywords (`datasets.py:1485`), register files and start uploads (`datasets.py:1394`, `gcs_storage.py:153`, `services/multipart.py:62`), start pipeline runs (`pipelines.py:101`), link any two same-project records (`link_utils.py:45-50`), create datasets in any project naming any owner (`datasets.py:185`, intended per `tests/test_create_on_behalf.py`), list members of every project (`projects.py:418`) and every user's project list (`users.py:986`). All of this is documented, but for self-hosting labs a per-instrument or per-project scope for SAs would materially reduce blast radius of one leaked instrument key. The operator-group binding exists precisely to enable that narrowing; it is currently "provenance only".
- Service accounts as owners. `transfer_ownership` (`resources.py:1153`) accepts a non-admin service account as the new owner of a project, dataset or instrument, and `create_project` accepts one as lead. The SA is capped at contributor, so the record ends up with an owner that cannot manage it and only a platform admin can recover. Apply `_validate_service_account_project_role`-style checks to new owners.
- Transfer to disabled accounts: `resolve_user_by_identifier` excludes pending but not disabled users (`user_utils.py:246-248`), so ownership can be transferred to a disabled account.
- `GET /datasets?keyword=` (`sql_utils.generate_kw_query`), `GET /users?q=`, `first_name`/`last_name` permissive filters and `/keywords/{k}` pass user text into `ilike` without escaping `%`/`_` (join requests correctly use `autoescape=True`). Not injection, but allows wildcard probing and expensive scans.
- `POST /users/resolve` and `POST /datasets/first_thumbnails` accept unbounded lists.
- The `public` column on `Resource`/`Dataset` is still written by `set_public` (compat) while every read uses `public_grant_exists`; once the rollout is done, drop the column to remove a second source of truth.
- `IngestionRequest` is returned as the raw table model (`ingestion_requests.py:158,203`), exposing `file_to_upload` (bucket and object path) and accepting the same model as PATCH body. A dedicated read/update schema would be safer against future columns.
- `PATCH /datasets/{mfid}`, `PATCH /samples/{mfid}`, `POST/PATCH /resources/{mfid}/metadata` do not call `assert_not_pending_deletion`, unlike the other write paths listed in `docs/database_deletion_contract.md:27`.
- Dataset creation lets any contributor attach the dataset to any registered instrument (`datasets.py:201-224`) without any right on the instrument, whereas re-assignment needs `MANAGE_ACL` on the dataset. This pollutes an instrument's dataset list but grants nothing new (operator SAs already have global contributor).
- Missing tests: no HTTP tests cover `/keywords`, `publish_resource` on a project, instrument `include_metadata` visibility, cache invalidation on membership change, or a service account filing deletion requests on records it does not own. The matrix in `tests/test_permissions.py` is excellent for the pure model; route-level negative tests for the platform-contributor path are thin.
- `print.py` publishes labels for any well-formed MFID with no ownership check (documented rationale: trays are printed before upload). Acceptable, but worth a rate limit since it drives physical hardware.
- Deprecated admin-only ACL routes (`/resources/{mfid}/access_groups`) can still grant to custom and instrument groups, bypassing `_guard_grant`'s owning-project check; they are admin-only so not a vulnerability, but they let an admin create states the new routes cannot express or undo.

---

## 3. Examined and found sound

- Core model (`permissions.py`): MIN(standing, grant)/MAX over groups, thresholds, `can_grant` cap on both ends, service-account ceiling in `has_permission` and `permission_filter`, platform admin bypass separated from effective role, immutability of `THRESHOLDS`, public group injected at viewer only (world-writable unrepresentable).
- `permission_filter` correlates correctly and is applied on every list/search/facet/graph/link traversal I checked: `/datasets`, `/samples`, `/files`, `/ingestion_requests`, `/postprocessing_runs`, `/embeddings`, `/embeddings/search`, `/resources/metadata/search`, `/entity_graph_cte`, `/project_graph`, `/resources/{mfid}/graph` (nodes and induced edges both filtered), dataset/sample `include_links`, parents/children listings, `first_thumbnails`, `sample_mfid`/`dataset_mfid` cross filters (`select_active` with principal), facets via `resource_scope_predicates`.
- Access selectors: `accessible_to_user` restricted to self/admin/platform contributor and always intersected with the caller's own filter; `accessible_to_project` and `project_scope=shared|all` require membership or global read; `member_ref` uses the same rule.
- Cursors: HMAC over kind, sort, direction, filter digest and principal fingerprint; legacy unsigned cursors only act as a keyset start inside the already-filtered query; join-request and deletion-request cursors validate shape and types.
- Mass assignment: `DatasetUpdate`, `SampleUpdate`, `ProjectUpdate`, `InstrumentUpdate`, `AccountProfileUpdate`, `UserUpdate`, `ServiceAccountPlatformRoleUpdate`, `ThumbnailUpdate`, `DatasetInstrumentAssign` are `extra="forbid"` and omit owner/project/public/role fields; `refuse_public_change` blocks `public`; `IngestionRequest` PATCH pins `dataset_mfid`/`file_mfid`; file PATCH guards storage fields.
- Ownership transfer requires `TRANSFER` (owner), preview-by-default, demotes previous project owner, removes previous personal grant, enforces one owner via partial unique indexes.
- Project reassignment requires `MANAGE_ACL` on the record and `CREATE` in the target, and swaps the project grant.
- ACL routes: `MANAGE_ACL` plus `require_grant` cap, owner grants only via transfer, project grants refused (except public, L1), owning-project grant refused, service accounts refused from granting.
- Join requests: service accounts cannot request, non-members only, pending cap, visibility limited to requester/reviewers/admin, review requires editor+ with role cap, withdraw requester-only, row locking.
- Deletion review and hard delete: `DELETE`/`HARD_DELETE` thresholds, service accounts excluded by the ceiling, blockers re-checked under lock, audit log admin-or-submitter only.
- Links: same-project create needs CREATE+READ, cross-project EDIT+EDIT, unlink EDIT+EDIT, cycle check does not leak names, pending-deletion blocked; `None == None` project case correctly treated as cross-project.
- Instrument operator groups: only service accounts can be bound, humans rejected from instrument groups on the generic route, members listing excludes operator group and public, bound SA receives `role: null`.
- Users: platform role changes human-admin-only with step-up, last-admin protection, self-change blocked; email shown only to self/admin; non-admin directory scoped to shared groups except exact lookups (L4).
- Admin-only routes carry `require_platform_admin`: access group create/read/members, service account list/get/patch/create/rotate, deletion audit (all scope), user create, user API key routes, deprecated ACL routes.
- Privilege mode: explicit `elevated` refused for non-admins and service accounts; `normal` strips admin from the principal that routes see; audit log emitted for elevated requests.
- No SQL built from request strings: the only `text()` uses are constants or bound parameters (`search_utils.py:40`, `resources.py:200`, `cli/db.py`).
- Pending-deletion records: hidden from lists via `deletion_filter_for`, still reachable by exact MFID to readers by design, writes blocked on the documented paths.

# Security audit 3: files, storage, uploads and untrusted input

Repository: `/home/roncofaber/software/crucible-api/.claude/worktrees/selfhost`, branch `feat/password-links` (main plus one open PR), read-only.
Scope: storage adapters (`app/storage/`), multipart and legacy upload routes and services, downloads and signed URLs, file versions, restore and cleanup, hard deletion of stored objects, ingestion and pipeline publishing, thumbnails, printing, and the handling of untrusted input (JSON bodies, metadata, search, sorting, cursors, embeddings, response headers built from filenames).
Checked against OWASP API Security Top 10 (2023), ASVS 5.0 chapters V1, V2, V5 and V12, and the GCS and S3 guidance on signed URLs.

Reproductions live in `/tmp/claude-1000/-home-roncofaber-software-crucible-api/0f41fda4-3511-448e-aa7b-b547605f8c40/scratchpad/audit/test_repro_files_inputs.py` and ran green with:

```
PYTHONPATH=. uv run pytest -q -s -p tests.conftest --rootdir=. <scratchpad>/audit/test_repro_files_inputs.py
4 passed, 8 warnings in 36.70s
```

Nothing was contacted outside the machine; the tests use the repository's own in-memory SQLite fixtures and patch the Google token call.

## 1. Findings

### F1. High. No request body size limit anywhere; unauthenticated callers can make a worker buffer arbitrarily large JSON in memory

Evidence:

- `app/api/main.py:212-231` (`reject_unstorable_json`): for any request whose `Content-Type` is `application/json` or `*+json`, the middleware does `body = await request.body()` and `json.loads(body)` before routing, before authentication and regardless of whether the path exists.
- `Dockerfile:28-33`: gunicorn with `UvicornWorker`, no body limit (neither server has one by default).
- `deploy/compose/Caddyfile:13-27`: `request_body { max_size 8KB }` applies only to `/api/v3/auth/hub/*`; the general `handle_path /api/v3/*` block has no limit. Caddy's default is unlimited.
- `deploy/compose/README.md:325` ("Allow request bodies of a few megabytes") and the nginx example (`client_max_body_size 16m`) show the intent, but the bundled proxy does not implement it.

Attack: `POST /api/v3/anything` with `Content-Type: application/json` and a multi-GB body (or several concurrent ~500 MB bodies) from an unauthenticated client. Each worker reads the entire body into memory, then parses it into Python objects (several times the wire size). With `WEB_CONCURRENCY=2` on the self-hosted bundle, two concurrent requests take the API down (OOM or swap). The same holds for real routes: FastAPI parses the JSON body before dependencies such as `authorize_user` run.

Reproduction (`test_unauthenticated_json_body_is_fully_buffered_before_routing`, `test_unauthenticated_body_is_parsed_before_auth`): a 48 MiB JSON array posted without credentials to `/no/such/route` returned 404 with a traced peak of 295 MiB; a body containing `\u0000` to the same unknown route returned the middleware's 422, proving parsing precedes routing and auth.

What does not stop it: the h11 header limit only bounds headers and the request line; `tests/test_request_body_and_log.py` covers nesting depth and ASCII scan speed, not size; the metadata routes (`app/api/routes/resources.py:441-537`) take a bare `dict` with no size cap, so an authenticated contributor can also store tens of MB of JSONB per resource, which the GIN expression index `ix_scientific_metadata_fts` (`app/models/core_models.py:556-562`) then runs `to_tsvector` over on every write (and fails with a tsvector size error above about 1 MB of text, surfacing as a 500).

Confidence: confirmed by reproduction (memory buffering and pre-auth parsing); the production impact depends on the proxy in front, which is unlimited in the bundle and unknown for the Foundry's GKE ingress.

Fix: add `request_body { max_size 16MB }` (or similar) to the `/api/v3/*` block of the Caddyfile and state the limit in the README's proxy rules; in the app, read `Content-Length` in `reject_unstorable_json` and return 413 above a configured ceiling, and stream-reject bodies without a length (ASGI `receive` loop with a running total) so the limit holds without a proxy; cap scientific metadata payloads explicitly (for example 1 MB serialized) and return 413/422 rather than letting the index fail.

### F2. High (Foundry GCS deployments only). Legacy upload routes let a CREATE-level contributor overwrite or delete any stored object of a dataset, bypassing EDIT, If-Match and version retention

Evidence:

- `app/api/routes/gcs_storage.py:131-176` (`_validate_and_prepare_upload`): only the dedup case (same filename, same hash) short-circuits. A filename that already exists with a different hash is accepted; there is no `filename_exists` 409 as in the multipart route (`app/services/multipart.py:187-188`).
- `app/api/routes/gcs_storage.py:229-238`: the resumable session is opened by the server's identity for `name = {dsid}/{filename}`, so finishing it overwrites that object.
- `app/api/routes/gcs_storage.py:109-128` (`_mint_upload_token`): the vended token carries `roles/storage.objectAdmin` on the whole `{dsid}/` prefix (except `_files/`), so the holder can overwrite or delete every legacy object of the dataset for the token's lifetime (one hour), not only the file being uploaded; with `LEGACY_UPLOAD_TOKEN_DOWNSCOPE=false` the raw service account token is returned instead (`:115-117`), which is bucket-wide.
- `app/api/routes/gcs_storage.py:299-368` (`complete_resumable_upload`): registers a second `AssociatedFile` with the same filename (no unique index for `storage_backend = 'gcs'`, `app/models/core_models.py:63-66`), with client-claimed `sha256_hash`, no generation, and never calls `retire`/`keep_version`, so the previous content is gone unless the bucket itself has versioning.

Attack: a contributor on a dataset (CREATE only) calls `POST /datasets/{dsid}/upload/initiate/multipart` with `filename` equal to an existing legacy file (`storage_path = bucket/{dsid}/{filename}`) and gets a token; with it they overwrite or delete that object directly in GCS, or they use `upload/initiate` and finish the resumable session over it. The existing record now points at other content with a stale size/hash, no `FileVersion` row exists, the ETag concurrency contract of the replace route is bypassed, and the EDIT requirement of `POST /files/{mfid}/upload/multipart/initiate` is never checked. The token also lets them upload unregistered objects of any size under `{dsid}/`, bypassing `MAX_UPLOAD_BYTES` and the multipart quotas.

Reproduction (`test_legacy_upload_initiate_targets_an_existing_files_object` vs `test_multipart_initiate_refuses_the_same_request`): with a dataset holding `data.h5` (sha `aaaa...`), a contributor's legacy initiate for `data.h5` with sha `bbbb...` returned 200 with `object_key == "ds_1/data.h5"` and minted a token for `ds_1`; the same request to the multipart route returned 409 `filename_exists`.

What does not stop it: the routes are marked deprecated and send a `Deprecation` header, but they are mounted (`app/api/main.py:331`) and only refuse when `STORAGE_BACKEND != gcs` (`_legacy_upload_bucket`), so they are live at the Foundry; the downscoped boundary excludes only `_files/`, not the other objects of the dataset; the multipart `filename_reserved` check covers only active multipart sessions.

Confidence: confirmed by reproduction up to token minting (the GCS write itself is not reproducible offline); the objectAdmin grant and resumable overwrite follow from the code and Google's documented semantics.

Fix: refuse `initiate` and `initiate/multipart` when an `AssociatedFile` with that filename already exists in the dataset (mirror `filename_exists`), and refuse `complete` when a same-name record appeared meanwhile; narrow the credential access boundary to the single object `{dsid}/{safe_filename}` (`resource.name == '...'`) with `roles/storage.objectCreator` rather than `objectAdmin` on the prefix; or disable the legacy routes behind a setting now that nano-crucible 3.3.0 uses multipart. Also quote or validate `dsid` before interpolating it into the CEL expression (see A3).

### F3. Medium. Deleted file content is never removed from storage and can be re-attached by a contributor (GCS backend)

Evidence:

- `app/api/routes/files.py:159-176` (`DELETE /files/{id}`) and `app/utils/deletion_utils.py:194-225` (`_hard_delete_dataset`) delete database rows only; `_delete_gcs_blobs_for_dataset` (`deletion_utils.py:163-193`) has no callers. No `FileVersion` row is written for a deleted file, so `storage cleanup` never reaches the object. Documented at `app/api/routes/deletion.py:742` and `docs/multipart_upload_api.md:44`.
- `app/api/routes/gcs_storage.py:55-88` (`_determine_storage_path`, `_ensure_storage_path`): a `storage_backend = "gcs"` record without `storage_path` is backfilled on download by listing `{dsid}/` in the bucket and matching the exact name, else any single object whose name ends with `/{basename}`.
- `app/api/routes/datasets.py:1376-1439` (`POST /datasets/{dsid}/files`) lets a CREATE-level caller create a `gcs` record with any `filename` and no `storage_path`; `AssociatedFileCreate` validates filenames only for non-GCS backends (`app/models/datasets.py:296-302`).

Scenario: a dataset admin deletes `results/secret.csv` (DELETE requires admin); the object stays in the bucket indefinitely. Any contributor on the dataset later posts a catalog record `{"filename": "secret.csv", "size": 1, "sha256_hash": "x"}` and requests `GET /files/{new}/download_link`; the backfill matches `{dsid}/results/secret.csv` by basename and signs a download URL for the deleted content. The same mechanism attaches the wrong object when two names share a basename. For hard-deleted datasets, the content stays in the bucket after the dataset, its ACLs and its audit record are gone, which is a retention and privacy gap for a deletion feature meant to honour deletion requests.

What does not stop it: `resolve_storage_object` confines downloads to the dataset prefix, which this stays inside; the contributor already has READ on the dataset, so the exposure is to deleted content, not other datasets. On the S3 backend `_determine_storage_path` returns 404, so only the retention part applies there.

Confidence: likely (code paths read end to end; not exercised against a bucket).

Fix: on `DELETE /files/{id}` and dataset hard deletion, write a `FileVersion` (reason `deleted`) for owned objects so `storage cleanup` removes them after the grace period, or call `retire`/`delete` directly; restrict the backfill to the exact `{dsid}/{filename}` match and drop the basename fallback (or make it an operator CLI step); validate GCS filenames on `AssociatedFileCreate` as `MultipartInitiate.valid_filename` does.

### F4. Medium. Ingestion and postprocessing messages carry unvalidated user strings to consumers

Evidence: `app/api/routes/files.py:181-252`: `ingestion_class: Optional[str]` is a free query parameter (no length, no allow-list) copied into the RabbitMQ message and into `IngestionRequest.ingestion_class`; `app/api/routes/ingestion_requests.py:435-466`: `PATCH /ingestion_requests/{id}` takes the full table model, so an editor can set `file_to_upload`, `ingestion_githash`, `status` and timestamps to arbitrary strings.

Scenario: the consumer (another repository) selects the ingestion code by `ingestion_class`; if it does so by dynamic lookup or import, a contributor controls that choice. The API's own handling is safe (bound parameters, JSON encoding), so the risk is a confused-deputy one across the queue.

What does not stop it: `file_to_upload` in the message is the validated `storage_path`; the routing key comes from settings and the size tier only; the consumer code is outside this repository so its behaviour could not be checked.

Confidence: possible.

Fix: validate `ingestion_class` against a server-side allow-list (as `PIPELINES` does for postprocessing) or at least `^[A-Za-z0-9_.-]{1,64}$`; replace the table-model body of `PATCH /ingestion_requests` with an update schema limited to `status`, `time_completed` and `ingestion_githash`.

### F5. Low. Hard deletion and file deletion race with in-flight replace and multipart sessions only partly

Evidence: `app/utils/deletion_utils.py:210` deletes `UploadSession` rows for the dataset inside the hard-delete transaction; `app/services/multipart.py:419-420, 209-217` treat a missing session as `dataset_deleted` and abort the provider upload; `app/services/multipart.py:434-441` treats a deleted replace target as `file_changed` and cleans the new object. These paths are correct. The remaining gap: `DELETE /files/{id}` (`files.py:159`) does not check for a live replace session, so the replace fails late with 409 after the whole upload, and the deleted file's object is orphaned (F3).

Confidence: likely; low impact (wasted upload, orphan object).

Fix: refuse `DELETE /files/{id}` with 409 `replace_in_progress` while a live session targets the file, as `restore_latest` does.

### F6. Low. Idempotency-key collision returns 500

Evidence: `app/services/multipart.py:151-163` (`new_session`) inserts under `uq_upload_session_idempotency` without catching `IntegrityError`; two concurrent initiates with the same key from the same user race past `idempotent()` and one gets a 500 (the user row lock in `reserve` serializes callers with an internal user id; API keys of principals without `caller_row` could still collide). `app/api/routes/pipelines.py:116-120` handles the same case correctly.

Confidence: possible.

Fix: catch `IntegrityError`, roll back and re-run `idempotent()`.

### F7. Info. Claimed SHA-256 is never verified server-side

Evidence: `sha256_verified_at` is only ever set to `None` (`files.py:146`, `multipart.py:457`, `file_versions.py:90`); no code sets it. Integrity on upload rests on CRC32C (provider-enforced per part and compared on completion, `multipart.py:286-331`), which is sound against corruption but not against a client lying about its own SHA-256. Dedup paths that require `sha256_verified_at` (`multipart.py:184, 271`) therefore never trigger, while `trusted_hash()` (`multipart.py:112-113`) treats legacy records without a generation as trusted.

Fix: either run a verification job that fills `sha256_verified_at` or document the hash as client-asserted in the API model; drop the `storage_generation IS NULL` branch from `trusted_hash()`.

### F8. Info. Bucket names are exposed to every reader and in error details

Evidence: `AssociatedFileRead.storage_path` (`app/models/datasets.py:311`) returns `bucket/key` to any dataset reader; `UploadMultipartInitiateRead.bucket` likewise. Error details are otherwise clean (codes and reasons only; `_determine_storage_path` 409 names only the basename). Exposing the bucket is a design choice the clients rely on; it is noted for completeness, not as a defect.

## 2. Areas of improvement (not vulnerabilities)

- A1. Filename validation is uneven: `MultipartInitiate.valid_filename` (controls, backslash, dot segments) is not applied to `AssociatedFileCreate` for GCS files nor to `AssociatedFileUpdate.filename` (`files.py:127-132`), which accepts any non-empty string for a GCS file. Downloads are safe because `download_headers` (`storage_downloads.py:90-99`) takes the basename and percent-encodes it, but the stored value feeds `retire_if_eligible` and the backfill. Apply one validator everywhere.
- A2. `GET /datasets/{dsid}/files` (`datasets.py:1356`) returns an unbounded list; `POST /datasets/first_thumbnails` (`datasets.py:1104`) accepts an unbounded `dataset_ids` list and only logs above a threshold. Cap both (known pagination debt).
- A3. `dsid` from the URL is interpolated into a CEL expression in `_mint_upload_token` (`gcs_storage.py:118-124`) and into object keys. It is safe today only because the dataset must exist and `DatasetCreate.unique_id` is validated as an MFID; validate the path parameter shape (`is_valid_mfid`) in `DatasetMfid` or escape it, so the guarantee does not depend on a lookup elsewhere.
- A4. `GET /resources/metadata/search` `q` has `min_length=3` but no `max_length`; URL length (h11, 16 KB) is the only bound. Add `max_length` and consider the token limit `normalize_search_query` already applies to the trigram search.
- A5. `print.py`: `BarcodePrintRequest.name` is unbounded and goes to the label printer verbatim; add a length cap. `printer_id` and `mfid` are validated well.
- A6. Hard deletion and file deletion should log the orphaned `storage_path` values (or write `FileVersion` rows) so operators can reconcile storage; today `storage cleanup` cannot find them.
- A7. `_ensure_storage_path` lists the whole `{dsid}/` prefix on every download of a path-less record and on `download_links` for the dataset; for large datasets this is slow and costs a GCS list per request. Backfill once in a migration.
- A8. The `assert_not_pending_deletion` guard is applied to uploads and metadata but `GET /files/{id}/download_link` still serves files of datasets with a pending deletion request (readers filtered by `select_readable_file` which does not apply `deletion_filter_for`, `sql_utils.py:284-294`; `list_files` does). Decide whether pending-deletion datasets should still be downloadable and make the two consistent.
- A9. Missing tests: a legacy-route test that an existing filename is refused (currently none, F2); a test that `DELETE /files/{id}` leaves no downloadable object or writes a version (F3); a body-size test once a limit exists (F1); a test that `reject_unstorable_json` does not read bodies for routes that do not exist.
- A10. `AssociatedFileCreate.size` and `sha256_hash` take any int and any string; for `gcs` records a 64-hex pattern and `ge=0` would catch client bugs early.

## 3. Examined and found sound

- Storage key construction: multipart objects are `{dsid}/_files/{uuid4}` (`multipart.py:158`), never derived from the filename; `resolve_storage_object` (`storage_downloads.py:54-70`) confines downloads and ingestion to allowed buckets, the file's own dataset prefix, no `.`/`..` segments, and requires a generation for objects under `_files/`. `owned_object` restricts delete and retire to the environment's own upload buckets. Tests in `tests/test_storage_downloads.py` cover these.
- Signed URLs: downloads are GET, one hour, GCS generation-pinned, with `response-content-disposition` and type override (`gcs.py:195-208`, `s3.py:128-137`); part URLs are PUT, at most 15 minutes and never beyond session expiry, bound to the part's CRC32C and Content-Length (`gcs.py:140-148`, `s3.py:100-106`); S3 presigner uses the public endpoint only for signing. Script-capable types are forced to attachment even when `inline` is requested; the compose proxy adds CSP sandbox, nosniff and CORP on the files path.
- Response headers from filenames: `download_headers` uses the basename, replaces non-printable and quote/backslash characters in the ASCII fallback and percent-encodes `filename*`, so CR/LF and quote injection are not possible.
- Upload session ownership: every multipart route resolves the session and checks `principal.has_user_id(upload.user_id)`, the dataset in the path, the protocol, expiry and state (`multipart.py:69-97`), under `FOR UPDATE` for mutations; permissions are re-checked at completion in the publishing transaction with the platform-admin role demoted (`multipart.py:54-62`, `434-445`). Idempotency keys are scoped per user and dataset and fingerprinted.
- Quotas and geometry: per-owner and per-dataset session and byte quotas computed under user and dataset row locks; file size capped at 500 GiB and `MAX_UPLOAD_BYTES`; part numbers 1..10000; at most 64 parts per signing call with a per-session signing interval; part listings validated for order and bounds (`base.py:31-39`); completion checks part count, sizes, authorization and provider checksums before publishing.
- Replace, restore, cleanup: `keep_version` is written in the repointing transaction; restore locks file and version rows, refuses expired versions and running replaces; cleanup claims rows with `FOR UPDATE SKIP LOCKED`, re-checks that no file uses the path, and only deletes owned objects; GCS deletes are generation-matched; S3 keys are never reused so an unversioned delete cannot hit a newer object. Abort during completion is handled in both orders.
- Provider responses: GCS XML parsed with ElementTree after rejecting DOCTYPE/ENTITY and oversized bodies, redirects disabled, bucket must equal the configured bucket; boto3 configured with timeouts and bounded retries.
- SQL: all user input reaches SQL through bound parameters; `text()` is used only for constants and `set_config` with bound values; trigram search uses `literal(token).op("<%")` and `autoescape=True` for prefix matches; sort and direction are enums; the JSONB path expression is a constant.
- Cursors: HMAC-SHA256 over a protobuf payload bound to kind, sort, direction, filter fingerprint and principal; key ids validated; decode errors map to 422.
- Embedding search: 768 finite floats exactly, `k` at most 100, `probes` bounded, `resource_mfids` at most 100, permission filter applied in the query.
- Thumbnails: base64 and byte caps, allow-listed Pillow formats, decompression bomb warnings turned into errors, pixel cap, always re-encoded to 512px JPEG.
- SSRF: no server-side fetch of a user-supplied URL. OIDC issuers and redirect URIs come from configuration with https-only validation; the proposal lookup uses a configured URL; `S3_PUBLIC_URL` is configuration.
- Deserialization and templates: `yaml.safe_load` for the config file, JSON everywhere else, no pickle; Jinja2 templates render with Starlette's `select_autoescape()`; `auth.py` escapes values in its hand-built HTML.
- Error handling: unhandled exceptions produce Starlette's generic 500 (no debug mode); storage and signing failures are logged server-side and returned as 503 with fixed messages; hub-auth paths get a sanitized 500 and 422.
- Printing: `printer_id` restricted to `[A-Za-z0-9_-]{1,64}` before topic interpolation, `mfid` validated, TLS to the broker.

# Crucible API security audit, area 4: configuration, deployment, operations and supply chain

Repository: `/home/roncofaber/software/crucible-api/.claude/worktrees/selfhost`, branch `feat/password-links` (commit 8620638). Read-only review plus local, offline checks (`uvx pip-audit` on the exported lock, driving the ASGI app in-process). Nothing was started, built, deployed or contacted. References: OWASP ASVS 5.0 (V13, V14, V16), CIS Docker Benchmark, GitHub Actions hardening guide, OWASP Top 10 CI/CD, NIST SP 800-218.

Severity counts: critical 0, high 1, medium 4, low 7, info 3.

## 1. Findings

### H1. Unauthenticated clients can make the API buffer and parse request bodies of any size (memory DoS)

Severity: high. Confidence: confirmed (compose bundle); likely (GKE, depends on the front door, which lives in mf-crucible-deploy).

Evidence:
- `app/api/main.py:212-231` (`reject_unstorable_json`): for any `application/json` request, `body = await request.body()` reads the whole body into memory and `json.loads` parses it, before routing, before authentication, with no size check.
- `deploy/compose/Caddyfile:25-27`: `handle_path /api/v3/*` has no `request_body max_size`; only `/api/v3/auth/hub/*` is capped at 8 KB (lines 13-23).
- `Dockerfile:31-35`: gunicorn runs with no `--limit-request-*` body-related option (gunicorn cannot cap bodies anyway), and `deploy/compose/compose.yaml:70-98` sets no memory limit for `api`.
- Measured: driving the app in-process with an unauthenticated `POST /datasets` carrying a 64 MiB JSON string returned 401 only after peaking at 261 MiB of Python heap (`scratchpad/audit/body.py`).

Scenario: anyone who can reach `/api/v3/` sends a handful of concurrent multi-hundred-megabyte JSON POSTs (no credentials needed). Each uvicorn worker buffers and parses them; with the default two workers and no container memory limit, the API process is OOM-killed or the host swaps. The hourly `migrate`/cleanup services and Caddy share the host.

What does not stop it: Caddy has no default body limit; the middleware runs before `authorize_user`; there is no edge rate limit in the bundle; the README's own nginx example sets `client_max_body_size 16m` (`deploy/compose/README.md:337`), but the shipped Caddyfile does not.

Fix: add `request_body { max_size 16MB }` (or whatever metadata requests need) to the `/api/v3/*` handle in the Caddyfile and to the README's proxy rules; in `reject_unstorable_json`, refuse bodies whose `Content-Length` exceeds a limit (413) before reading, and stream-read with a cap for chunked bodies; add `mem_limit`/`deploy.resources.limits` to the API services in compose. Consider moving the JSON scan after authentication for authenticated routes.

### M1. API containers run as root with a writable root filesystem

Severity: medium. Confidence: confirmed.

Evidence: `Dockerfile:4-5` (`USER root`, `WORKDIR /root/`), no later `USER`; `deploy/compose/compose.yaml:6-9` (`x-api`) sets `cap_drop: [ALL]` and `no-new-privileges` but no `user:` and no `read_only: true`. The CI check at `.github/workflows/tests.yml:336` confirms the effective capability set is empty, which limits the blast radius but does not change the uid.

Scenario: a code-execution bug in the API (or a malicious dependency) runs as uid 0 inside the container, can rewrite `/root/app` and the venv so the backdoor survives restarts, and any future container-escape bug is a root escape. CIS Docker 4.1 / 5.12; ASVS 13.

What does not stop it: `cap_drop ALL` removes capabilities but a uid 0 process still owns every file in the image; `no-new-privileges` only blocks setuid escalation.

Fix: create an unprivileged user in the Dockerfile (`useradd --system crucible`, `COPY --chown`, `USER crucible`), move the app out of `/root`, set `read_only: true` with a `tmpfs: /tmp` in `x-api`. Note the same image is deployed on GKE; its `securityContext` is in mf-crucible-deploy and was not reviewed.

### M2. Default bundle serves plain HTTP on all interfaces and API keys cross the LAN in the clear

Severity: medium. Confidence: confirmed.

Evidence: `deploy/compose/.env.example:5,13` (`CRUCIBLE_HTTP_PORT=8080`, `CRUCIBLE_SITE_ADDRESS=:80`); `deploy/compose/compose.yaml:212-215` publishes `"${CRUCIBLE_HTTP_PORT:-8080}:80"` without a host address, so Docker binds `0.0.0.0:8080` (and adds iptables rules that bypass ufw-style host firewalls); `deploy/compose/README.md:42,74-83` tell people to connect clients with `crucible config set api_url http://<server>:8080/api/v3` and an API key.

Scenario: a lab keeps the defaults ("it works"), several workstations use the server by IP, and every request carries a long-lived bearer API key over plain HTTP on the shared LAN or Wi-Fi; anyone on the segment captures it. ASVS V13/V14 (transport).

What does not stop it: the password feature correctly refuses non-HTTPS (`app/config.py:662-668`), and the browser login cookie is `https_only` (`app/auth/browser_login.py:45`), but API keys have no such guard. The README says HTTPS is needed only "with the Hub".

Fix: bind the plain-HTTP port to loopback by default (`127.0.0.1:${CRUCIBLE_HTTP_PORT:-8080}:80`) and require an explicit choice to expose it; or make `CRUCIBLE_SITE_ADDRESS=localhost` (Caddy internal CA) the default; at minimum add a prominent warning in step 2 of the README that API keys travel in clear text until HTTPS is set up, and have `settings check`/startup log a warning when `API_PUBLIC_URL` is `http://` on a non-loopback host.

### M3. Plaintext API keys persist in the `apitoken` table and in every database backup; backup guidance does not encrypt

Severity: medium. Confidence: confirmed.

Evidence: `app/utils/auth_utils.py:129` still writes every new key in clear to `ApiToken.token`; `app/auth/api_keys.py:1-3` describes the dual-write; `deploy/compose/README.md:265-269` tells operators that "A database backup contains every user's API key" and to `pg_dump` into `~/crucible-backups` with `umask 077`, with no encryption step; `migrations/data/20261106_backfill_api_keys.py` copies hashes but nothing removes the plaintext.

Scenario: a backup disk, NAS share or `~/crucible-backups` folder is lost or read by another admin; every active API key is usable immediately, with no hash to crack. The same applies to Cloud SQL exports at the Foundry. ASVS V14 (data protection at rest, credentials stored as hashes).

What does not stop it: the hashed `api_key` table exists, but the plaintext column is still populated and still authoritative for adoption (`_adopt_plain_key`).

Fix: finish the migration (stop writing `apitoken.token`, null it after the backfill, drop the column), and until then document `pg_dump ... | age -r <key>` or `gpg -c` in the backup section and `--exclude-table-data=apitoken` where a backup need not keep keys (noting it loses nothing once keys are hashed).

### M4. Four historical data scripts write and commit without a `--commit` or production flag

Severity: medium. Confidence: confirmed (they still run; impact limited to operators with a prod `.env`).

Evidence: `migrations/data/20260512_backfill_storage_path.py:61,64`, `20260512_backfill_associatedfile_mfid.py:35`, `20260603_backfill_username_from_email.py:63`, `20260608_backfill_user_access_groups.py:52` call `session.commit()` unconditionally and define no `--commit`, `--dry-run` or `--yes-this-is-production` argument (grep in `migrations/data`). This contradicts `AGENTS.md` ("They must default to read-only or rollback behavior and require an explicit commit flag") and `migrations/README.md`.

Scenario: an operator re-running "the backfills" on a new installation or on production with `DB_CONF_FILE=.env.prod` points one of these at the live database; it rewrites rows with no dry run and no refusal.

What does not stop it: `get_engine()` has no production guard; the guard lives only in `app/cli/db.py` and `migrations/env.py`, which these scripts bypass.

Fix: either delete the four scripts (their work is done and in git history) or add the standard `--commit`/`--yes-this-is-production` arguments.

### L1. Request log lines can carry attacker-chosen text (inline log injection)

Severity: low. Confidence: confirmed.

Evidence: `app/api/main.py:101-103` (`_route_template` falls back to `request.url.path` for unmatched routes) and `:298-307` log it with `%s`. Driving the app with the percent-decoded path `/no-such-route\nINFO:app.api.main:DELETE /datasets/forged 200 ...` produced the log line `INFO:app.api.main:GET /no-such-routeINFO:app.api.main:DELETE /datasets/forged 200 1.000ms request_id=x actor=admin credential=api_key 404 ...` (`scratchpad/audit/loginj2.py`).

What limits it: Python's `urlsplit` strips CR, LF and TAB (`'/ab\x1b[31mRED\x08'` test), so a new line cannot be forged; h11 rejects raw control characters in the request line. Other control characters (ESC, backspace) pass and can corrupt a terminal or confuse grep-based parsing. ASVS V16.2 (log injection).

Fix: log `json.dumps(path)` or `repr()` for the fallback path (as `_deprecated_caller_agent` already does for the user agent), or log a fixed token such as `<unmatched>` plus the length.

### L2. `pyasn1 0.6.4` has four published CVEs (fixed in 0.6.5)

Severity: low. Confidence: confirmed version, likely low reachability.

Evidence: `uvx pip-audit` on `uv export --locked` reports CVE-2026-119194/5/6/7 for `pyasn1==0.6.4`, fix `0.6.5`; `pyproject.toml:52` pins `pyasn1>=0.6.4`; dependency path `google-auth -> pyasn1-modules -> pyasn1` (`uv tree --invert`). CVE-2026-119194 is quadratic CPU use decoding attacker-supplied BER/DER without a schema (CVSS 7.5, DoS).

Scenario: pyasn1 here decodes service-account PEM keys and Google certificates, which operators control, so an attacker-supplied ASN.1 blob is unlikely; the risk is mainly the audit noise and the Dependabot PR being skipped. Dependabot groups minor/patch updates weekly (`.github/dependabot.yml:7-9`), so this should self-heal.

Fix: raise the constraint to `pyasn1>=0.6.5` and run `uv lock`.

### L3. `/metrics`, `/health/ready`, `/docs` and `/openapi.json` are public and unauthenticated through the proxy

Severity: low. Confidence: confirmed.

Evidence: `deploy/compose/Caddyfile:25-27` forwards all of `/api/v3/*`; `app/api/main.py:312` exposes Prometheus metrics with no dependency; `app/api/routes/misc.py:93-127` returns `git_commit`, `branch`, `api_version`, `schema_revisions` and DB latency; FastAPI's default `docs_url`/`openapi_url` are left on (no `docs_url=None` anywhere; `README.md:56` advertises it).

Scenario: an anonymous visitor learns the exact commit, migration state, per-route request counts and latency (handler labels are route templates only, so no user data), and the full route surface. This aids targeting but leaks no secrets; ASVS 13.3.

What limits it: `should_group_untemplated=True` means unmatched paths are labelled `none`, so metrics labels cannot be polluted with user-chosen paths.

Fix: in the Caddyfile, restrict `/api/v3/metrics` to loopback or the monitoring network (`@metrics path /api/v3/metrics` + `remote_ip`), or require a bearer token; consider keeping `/health/live` public and moving the provenance detail behind auth; leave docs as a deliberate choice.

### L4. No HSTS or baseline security headers on API responses when HTTPS is enabled

Severity: low. Confidence: confirmed.

Evidence: `deploy/compose/Caddyfile` sets headers only on the `/crucible-files/*` handle (lines 36-40); the API sets `X-Content-Type-Options` only for Hub-auth routes (`app/api/main.py:59-64`) and CSP only on its HTML pages. Caddy does not add `Strict-Transport-Security` by default.

Scenario: a user who once typed `http://crucible.mylab.org` keeps being redirected each visit and can be downgraded by an on-path attacker on the first request; JSON responses lack `nosniff`. ASVS 13.x / OWASP headers.

Fix: add a site-level `header { Strict-Transport-Security "max-age=31536000; includeSubDomains" X-Content-Type-Options nosniff Referrer-Policy no-referrer }` guarded by a `@https` matcher (so the `:80` default does not emit HSTS), and set `X-Content-Type-Options: nosniff` in `log_requests` for all responses.

### L5. `FORWARDED_ALLOW_IPS="*"` trusts every peer on the shared, non-internal `crucible` network

Severity: low. Confidence: confirmed configuration; spoofing from the internet is prevented.

Evidence: `deploy/compose/compose.yaml:78-79`; `app/auth/throttle.py:81-84` keys the password throttle and the `auth_event.client_address` on `request.client.host`; the `crucible` network (`compose.yaml:241-243`) is not `internal` and is designed to be joined by the Hub project and by name by anything else on the host.

What stops external spoofing: Caddy ignores client-supplied `X-Forwarded-*` when the client is not a trusted proxy (Caddy docs, reverse_proxy; `trusted_proxies` is unset here), and the API publishes no port. So an internet client cannot forge its address. The residual risk is any other container an operator attaches to `crucible` (or the Hub itself) reaching `api:8000` directly and setting arbitrary `X-Forwarded-For`, which defeats the per-address login throttle and falsifies the sign-in history.

Fix: set `FORWARDED_ALLOW_IPS` to the proxy's (and Hub's) container addresses using a static subnet, or put the API on a dedicated `edge` network shared only with the proxy and give the Hub its own.

### L6. Compose services have no resource limits

Severity: low. Confidence: confirmed.

Evidence: no `mem_limit`, `cpus`, `pids_limit` or `deploy.resources` in `deploy/compose/compose.yaml`.

Scenario: combined with H1 (or a SeaweedFS compaction burst, or Argon2 hashing at 64 MiB per check with `hash_concurrency` 2 per worker), one container can take the whole host's memory and the OOM killer picks the database. CIS Docker 5.10/5.11/5.28.

Fix: add `mem_limit` and `pids_limit` per service with values documented in `.env.example`.

### L7. `uvicorn.workers.UvicornWorker` is deprecated upstream

Severity: low (maintenance). Confidence: confirmed.

Evidence: `Dockerfile:32`; importing `uvicorn.workers` with uvicorn 0.54.0 raises `DeprecationWarning: The uvicorn.workers module is deprecated. Please use uvicorn-worker package instead.`

Scenario: a Dependabot bump that removes the module breaks the image at startup; staging would catch it, but a self-hoster pulling `main` would not.

Fix: add `uvicorn-worker` to dependencies and use `uvicorn_worker.UvicornWorker`, or run uvicorn directly with `--workers`.

### I1. Build-time images not pinned by digest

Severity: info. `Dockerfile:1-2` (`python:3.11-slim-trixie`, `ghcr.io/astral-sh/uv:0.11.16`), `cloudbuild.yaml:6` and `cloudbuild.release.yaml:6` (`gcr.io/cloud-builders/docker`, `cloud-sdk:slim`, floating), `compose.yaml` service images by tag only, `.github/workflows/tests.yml:23` (`pgvector/pgvector:0.8.1-pg17` by tag while the SeaweedFS/Dex/Versity test images are digest-pinned). Dependabot's docker and docker-compose ecosystems cover the first two sets. SSDF PS.1/PO.3: consider `@sha256` pins with Dependabot keeping them fresh, especially for the release copy job.

### I2. `settings check` is Kubernetes-only

Severity: info. `app/cli/settings.py` reads rendered Kubernetes manifests; there is no equivalent that reads a Compose project (`docker compose config`). Self-hosters get their first feedback at container start. Not a vulnerability.

### I3. Public ORCID client id in `.env.example-localdev` and `app/conf/*.yaml`

Severity: info. `APP-E5VUS6XSJS5VFNEN` is a public OAuth client identifier, not a secret; no secret values were found in tracked files (regex sweep over `git ls-files` for private keys, cloud keys, DSNs with passwords; `git log --all --diff-filter=A` shows only the two `.env.example*` files ever added). The CI OIDC fixtures use labelled test-only passwords (`tests/fixtures/dex.yaml`).

## 2. Areas of improvement (not vulnerabilities)

- Add a CI smoke step that confirms anonymous access to the bundled store is refused through the proxy (`GET /crucible-files/` list, `GET /crucible-files/<known key>` without a signature, `PUT` without a signature). The current smoke tests prove signed flows work and the Hub cannot reach the store, but not that unsigned requests fail (`.github/workflows/tests.yml:206-214, 313-338`).
- Add a CI step asserting the API container's uid is non-zero and its root filesystem is read-only once M1 is fixed, next to the existing CapEff assertions.
- `migrations/env.py` sets `lock_timeout` 5s and `transaction_per_migration`, which is good; 20 of 40 index-creating revisions use `CONCURRENTLY`. A short note in `migrations/README.md` on when to use `postgresql_concurrently` for large tables would help future revisions.
- Backups: the README restores with `pg_restore --clean --if-exists` into the live database; recommend restoring into a fresh database first, and documenting a restore drill. Also mention excluding or encrypting `apitoken` (M3).
- The hourly cleanup loops use `sleep 3600` in `sh` (`compose.yaml:105,128`); a failure of `python -m app.cli storage cleanup` is only visible in logs. A healthcheck or a "last success" file would surface silent failures.
- `log_requests` logs `actor=<user_id>` where `user_id` is the ORCID or MFID of the caller; acceptable for an audit trail, but note it for log retention policies (ASVS 16.3: personal identifiers in logs). The `auth_event` table stores raw `client_address` text (`core_models.py:383`) while the throttle table stores a keyed hash; consider the same keyed hash or a documented retention (currently 365 days via `CRUCIBLE_AUTH_EVENT_RETENTION_DAYS`, which is reasonable).
- Add `pip-audit` (or `uv audit` when available) and a Caddyfile/compose lint to CI, so the next pyasn1-style advisory fails a PR rather than waiting for the weekly Dependabot run.
- Consider `Server` header suppression (`server_header=False` for uvicorn) and a `Permissions-Policy` on the HTML pages; purely hardening.
- `deploy/compose/README.md:24` says `chmod 644 secrets/*`; the `700` directory makes this safe, but a one-line explanation of why (the postgres container reads them as uid 999) would keep operators from "fixing" it with `chmod 600` and breaking `db`.

## 3. Examined and found sound

- Secret loading (`app/config.py:46-126, 585-649`): one-way-only rule, blank-as-unset, `_FILE`/secrets-dir/env/Secret Manager precedence, error messages never include values, `SecretStr` for every secret, ASCII/length checks for secrets sent as HTTP Basic, startup `read_secrets()` so a worker fails fast.
- Startup checks (`check_settings`): required-setting enumeration per enabled feature, HTTPS required for passwords, mutual exclusion of `CRUCIBLE_AUTH_CLIENTS` and `HUB_AUTH_ENABLED`, OIDC issuer and redirect URI validation (https only, no userinfo/query/fragment, loopback rules per RFC 8252).
- Error handling: `RequestValidationError` on hub-auth routes is replaced with a generic 422 and `no-store` headers; unhandled exceptions on those routes return a generic 500 with only a request id; FastAPI's default 500 elsewhere returns "Internal Server Error" without a traceback. Uvicorn access log disabled so OAuth codes in query strings are not logged; `log_requests` logs method, route template, status, request id, actor id and credential kind only.
- Logging content: no token, key, password, email or request body is logged anywhere found (grep of all `logger.*` calls); failures log exception class names, not messages; `record_event` accepts no free text by design; `auth_event` rows are purged hourly after `CRUCIBLE_AUTH_EVENT_RETENTION_DAYS`.
- Metrics labels: route templates, grouped status codes, untemplated paths collapsed to `none`; no user data.
- Compose topology: database on an `internal` network with the API and migrator only; SeaweedFS on an `internal` network with the API and proxy only; no published ports except Caddy's; `cap_drop: [ALL]` and `no-new-privileges` on API, proxy (only `NET_BIND_SERVICE` added), SeaweedFS and rclone; SeaweedFS runs as uid 1000; Postgres drops `NET_RAW`; secrets are Docker file secrets, never environment variables; `secrets/` and `.env` git-ignored at both levels; healthchecks and `depends_on` conditions ordered correctly (`migrate` before `api`, `api` before `proxy`).
- Caddyfile: strips `Authorization` and `Cookie` before the store; CSP sandbox, `nosniff`, CORP on downloads; records bucket not routed; 8 KB body cap and 20 s upstream timeout on Hub-auth routes; `/hub/*` returns 404 when no Hub is configured.
- Proxy trust: Caddy drops client-supplied `X-Forwarded-*`, the API is not published, so `FORWARDED_ALLOW_IPS="*"` cannot be abused from outside (see L5 for the in-network residual).
- Migrations: production guard (`ALEMBIC_PRODUCTION_DATABASE`, `-x yes-this-is-production`) in both `app/cli/db.py` and `migrations/env.py`; advisory lock against concurrent upgrades; refusal of a database with tables but no `alembic_version`; `lock_timeout` 5 s; per-migration transactions; baseline applied atomically; readiness reports `behind`/`ahead`/`mismatch` against the code's heads.
- CLI: `storage cleanup` is a no-op unless `STORAGE_CLEANUP_ENABLED=true` and has `--dry-run`; `auth cleanup --dry-run`; `auth end-sessions` is a dry run unless `--commit`; `identity move` asks or requires `--yes`; `admin create` refuses a second admin without `--allow-additional`, asks for confirmation, prints the key once to stdout with a warning on stderr; `admin set-role` refuses to demote the last admin; `admin password-link` requires passwords enabled and an https public URL. Recent data scripts (2026-08 onward) default to read-only with `--commit`/`--yes-this-is-production`.
- GitHub Actions: `permissions: contents: read` at the top of every workflow; only the openapi release step escalates to `contents: write` and only on `push main`/`release`; no `pull_request_target`, no `workflow_run`, no secrets referenced; `persist-credentials: false` on every checkout; every action pinned to a full SHA with a version comment; Dependabot covers uv, docker, docker-compose and github-actions weekly; CI databases and S3 keys are labelled throwaway values; the compose smoke test deletes the admin key file; the Hub checkout is pinned to a commit and its image to a digest.
- Release pipeline: `cloudbuild.release.yaml` copies the exact digest staging ran, verifies the tag matches `pyproject.toml`, and re-reads the copied digest; no rebuild at release time (SSDF PS.3).
- Image: `uv sync --locked --no-dev`, `.dockerignore` excludes `.git`, `.env*`, tests, docs, `migrations/data`, deploy files and dumps; no secrets baked in; only `scripts/cleanup_hub_auth.py` from `scripts/` is copied.
- Dependencies: lock file committed; `pip-audit` finds only the pyasn1 advisories (L2); `starlette>=1.7.0`, `certifi>=2026.10.19`, `anyio`, `idna`, `mako` constrained explicitly.
- Health routes: `/health/live` is a cheap liveness; readiness touches the DB with a timing and never includes connection details or secrets.
- Repository hygiene: no secret material in tracked files or history (see I3); `.gitignore` excludes `.env*`, `secret`, `*.dump`, `/*.csv` (after the PII export incident).
