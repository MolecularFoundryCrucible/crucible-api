# Self-hosting Phase 3: sign-in plan

Status: approved by the user on 2026-11-02, after an independent review against the code. It replaces the Sign-in section of `docs/self_hosting_design.md`. The user's decisions are recorded at the end.

## Goal

Crucible speaks OIDC and also has its own password sign-in. A small lab runs on passwords alone with no outside service. Any lab can add ORCID, an institution's login, or both, side by side, and one person can link several of them to one account. A group that wants more (registration, multi-factor) points Crucible at Authentik or Keycloak through the same OIDC path. Permissions and API keys stay in Crucible in every case.

The Foundry keeps ORCID with passwords off. Nobody at the Foundry notices a change in how they sign in, and existing user identifiers do not change.

## What changed from the earlier design

- Generic OIDC support was out of scope; it is now the core of this phase.
- Administrators no longer hand out temporary passwords. They issue a one-time link, so no administrator ever knows a user's password.
- A pepper was optional, then deferred; it is now proposed on by default.
- Local sign-in is one method among several, not a special lab mode.
- Registration is a setting (`closed`, `approval`, `open`), not left for later.

## Why this shape

Most comparable software builds in passwords and adds providers as an option: InvenioRDM, Dataverse, SciCat, Girder, Gitea, Grafana, Nextcloud and Immich. NOMAD is the close match that bundles Keycloak instead; its local mode ships default credentials and its docs recommend their central service. Authentik was tried locally on 2026-11-02 and works through the OIDC path, so it stays available as a plug-in, not a required part of the install.

## How sign-in works

1. A client (the Hub, or the browser page that shows an API key) starts sign-in at the API.
2. If only one method is enabled, the API goes straight to it, so the Foundry sees no new page. Otherwise it shows a small page listing the enabled methods.
3. A provider method runs the OIDC code flow. The password method shows a form served by the API.
4. Every path ends in one resolver, which decides who this person is and when they authenticated, then continues the existing handoff. The Hub's tokens and refresh are unchanged.

**One resolver, no exceptions.** Today `/auth/apikey` (`app/api/routes/auth.py:117`) and `create_handoff_code` (`app/utils/hub_login.py:146`) each create users on their own. Both move onto the resolver, so account creation, `disabled_at` and the identity rules apply everywhere, with a test for each entry point.

`create_handoff_code` currently refuses anything that is not an ORCID (`hub_login.py:141`), and `load_login_config` requires ORCID issuers and Hub secrets (`hub_login.py:63`); both change. The login routes stay under `/auth/`, and the existing protections move with them: the login cookie's path, the safe validation errors (`_is_hub_auth_request`, `app/api/main.py:65`), and Caddy's request size and timeout limits, which cover only `/auth/hub/*` today.

## Code organization

Everything to do with sign-in lives in `app/auth/`, with one module per concern. Routes in `app/api/routes/` stay thin, as in FastAPI's full-stack template, and settings live in `app/config.py` (Twelve-Factor: strict separation of config from code). Domain packages follow the layout used by Polar and Netflix Dispatch. The `hub_*` files and `auth_utils.py` move out of `app/utils/` as later tasks touch them.

## Data model

New tables reference `user.id`, the integer key, as `UserPreferences` already does, and never `user.orcid`. `user.unique_id` stays as the public identifier and never changes. Moving the older foreign keys to `user.id` and renaming `orcid` API fields waits for 4.0.

New code never treats `unique_id` as an ORCID and never uses it to decide who someone is: sign-in goes from `user_identity` (`issuer`, `subject`) to `user.id`, and a person's ORCID is read from `user_identity`. The user expects every person to get an MFID eventually. That move is a separate project, but this rule keeps sign-in out of it.

### `user_identity`: which provider accounts belong to which user

| Column | Type | Notes |
|---|---|---|
| `id` | bigint, primary key | |
| `user_id` | int, not null, FK `user.id`, on delete cascade, indexed | |
| `issuer` | text, not null | the exact `iss` value, not a config name |
| `subject` | varchar(255), not null | the exact `sub` value |
| `label` | varchar(255), null | shown to the user, such as a username at the provider; never used to decide anything |
| `created_at` | timestamptz, not null | |
| `created_by_user_id` | int, null, FK `user.id`, on delete set null | set when an administrator made the link |
| `verified_at` | timestamptz, null | when the provider itself confirmed this login, at sign-in or linking. Empty for logins an administrator entered and for backfilled rows until the person next signs in. Any provider can use it |
| `last_used_at` | timestamptz, null | |

Constraints: unique (`issuer`, `subject`), so one provider account belongs to one person. A partial unique index on `user_id` for the ORCID issuers, so a person has at most one ORCID; it may change later, as the user expects. Other issuers can have several, because one CILogon issuer can front two institutions.

Why these columns: OIDC Core section 5.7 says `iss` and `sub` together are the only stable identifier, and the OWASP Authentication cheat sheet says the same. Email and names are not stored here and never used to match. Provider tokens are not stored, because Crucible only uses the provider to sign in. Config names are not stored either, so renaming a provider in settings changes nothing; a provider's issuer changing does need a rewrite, which gets its own command (`auth rewrite-issuer`).

### `local_credential`: passwords and their failure state

| Column | Type | Notes |
|---|---|---|
| `user_id` | int, primary key, FK `user.id`, on delete cascade | one per user |
| `password_hash` | text, not null | PHC-format Argon2id string |
| `pepper_id` | varchar(32), null | which pepper was applied |
| `password_changed_at` | timestamptz, not null | |
| `failed_attempts` | int, not null, default 0 | consecutive failures from unknown browsers |
| `trusted_failed_attempts` | int, not null, default 0 | consecutive failures from a known browser or a signed-in session |
| `blocked_until` | timestamptz, null | |
| `locked_at` | timestamptz, null | set at the hard limit |

Separate from `user`, so ordinary user queries and API responses cannot touch the hash. Failure state lives here, on the account and not on its username, so a rename neither clears a lock nor passes it to the next holder of that name. A row exists only once the person has set a password. Service accounts can never have one, enforced in every code path that writes the table, with a test.

### `password_token`: one-time setup and reset links

| Column | Type | Notes |
|---|---|---|
| `token_hash` | varchar(64), primary key | SHA-256 of a 256-bit random token; the token itself is never stored |
| `user_id` | int, not null, FK `user.id`, on delete cascade | |
| `purpose` | varchar(16), not null | `setup` or `reset` |
| `created_by_user_id` | int, null, FK `user.id`, on delete set null | |
| `created_at`, `expires_at` | timestamptz, not null | |
| `consumed_at`, `voided_at` | timestamptz, null | |

A partial unique index on `user_id` where both `consumed_at` and `voided_at` are null means one live link per user, even under concurrent requests. A new link, disabling the user, a password change, or a change of linked identity voids the live one. Email reset, if added later, uses this same table.

### `login_throttle`: failures per client address

| Column | Type | Notes |
|---|---|---|
| `address_hash` | varchar(64), primary key | keyed hash of the client address |
| `consecutive_failures` | int, not null | |
| `blocked_until` | timestamptz, null | |
| `last_failure_at` | timestamptz, not null, indexed | |

In PostgreSQL because the API runs several workers. Unknown usernames need no counter of their own, because a delayed account answers exactly like a wrong password, so there is nothing to tell apart. Rows go when `last_failure_at` is 30 days old.

### `auth_event`: history

| Column | Type | Notes |
|---|---|---|
| `id` | bigint, primary key | |
| `at` | timestamptz, not null, indexed | |
| `event` | varchar(32), not null | sign-in, failure, lock, password set, changed or reset, link issued, identity linked or unlinked, API key created or revoked, provider refused, disabled, enabled |
| `user_id` | int, null, FK `user.id`, on delete set null, indexed with `at` | |
| `user_unique_id` | varchar, null | a snapshot, so the record survives removal of the user |
| `actor_user_id` | int, null, FK `user.id`, on delete set null | the administrator, when different |
| `issuer` | text, null | |
| `client_address` | text, null | |

Never holds a password, token or hash, and never what was typed in the username field, because people sometimes type their password there. A failure on an unknown name is recorded without a user. The application only ever inserts. Retention is a setting: addresses and failures 90 days, the rest one year. A table rather than log lines alone, because a small lab has no log system and an administrator needs to see this in the Hub. Each event also goes to the normal log.

### Changes to existing tables

- `user.pending_at`, a nullable timestamp, set on an account waiting for approval. Existing users stay null, so nothing is written to them. A pending user gets no session or API key, on any path.
- A provider-created user with an MFID gets a generated username, because nano-crucible v3.0 and v3.1 cannot look a user up by MFID.
- `user.disabled_at`, a nullable timestamp. There is no way today to switch an account off. It is checked on sign-in by any method, handoff code redemption, Hub refresh, Hub access token use (`app/utils/hub_tokens.py:174`), and API keys, within the auth cache lifetime.
- `HubLoginTransaction` gains `purpose` (`sign_in`, `link`), `provider_key`, and a nullable `user_id` for linking. `HubLoginCode` gains `purpose` and the identity being linked.
- `username` becomes required for anyone with a password, and for them a change needs a recent sign-in. Clearing it is refused. ORCID-created users have none today; one is generated when they add a password.

### Cleanup

The bundle has no job for Hub sign-in rows today: `cleanup_hub_auth` runs only as the Foundry's CronJob, and Compose runs only `storage-cleanup` (`deploy/compose/compose.yaml:102`). Phase 3 adds the new tables to that job, and adds it to the bundle.

## OIDC providers

Providers are a list in settings. Each has a key, issuer, client ID, client secret from the secrets folder, scopes, a display name, and these options:

- `registration`: overrides the deployment's registration setting for this provider (see Registration).
- `subject_is_orcid`: the subject is an ORCID iD. Allowed only for `https://orcid.org` and `https://sandbox.orcid.org`, and startup fails on anything else. ORCID status always comes from this flag, never from what a value looks like, because another provider could issue a `sub` shaped like someone's ORCID and claim their account or their proposals.

When no list is set and `hub.enabled` is on, the existing ORCID settings become one implicit provider, including the `ORCID_CLIENT_ID` fallback in `app/utils/hub_orcid.py:15`. The Foundry's actual environment gets checked before this ships, so its configuration does not have to change.

Rules, each with a test:

- Use Authlib's FastAPI integration: the `OAuth` registry with `server_metadata_url`, as its documentation describes. No hand-written URL building or token parsing. It provides `state`, `nonce`, discovery, PKCE, signing key refetch and ID token checks; Crucible adds only an exact issuer match and a filter to asymmetric algorithms.
- Read endpoints from the provider's discovery document on first use. Refuse the provider if the document's `issuer` is not an exact match for the configured one. Issuers must be HTTPS, except `localhost`.
- Accept only asymmetric signature algorithms the provider advertises; never `none` or HS256.
- Check signature, `iss`, `aud`, `exp` and `nonce` on every token. A token missing `sub` is refused.
- Always send `state` and `nonce`. Send PKCE (S256) when the provider advertises it. ORCID's discovery document does not list it, so `nonce` carries that protection there.
- **Mix-up defence.** With more than one provider, RFC 9700 section 4.4.2 requires one. ORCID does not advertise the `iss` response parameter, so each provider gets its own callback address (`/auth/hub/oidc/{key}/callback`), and the login record stores which provider it started with; a callback for any other provider is refused. Endpoints always come from that provider's own discovery document. The existing `/auth/hub/callback` stays as the callback for the implicit ORCID provider only, because ORCID matches redirect addresses exactly and the Foundry's is registered.
- **Recent sign-in.** To refresh it, the API sends `prompt=login` with `max_age`, because the local Authentik test ignored `max_age=0` alone. If the returned token has no `auth_time`, the sign-in is not treated as recent. The check and its messages stop saying ORCID.

### Who is this person

1. Look up (`issuer`, `subject`) in `user_identity`. If found, that is the user, unless they are disabled: then refuse and stop, and never continue to the later steps.
2. If not found and the provider has `subject_is_orcid`, and a user already has that ORCID as `unique_id`, refuse with a message saying an administrator can help. This is the case of an ORCID unlinked from its account: it stays unlinked, and no duplicate is attempted against the unique column.
3. If registration for this provider is `open` or `approval`, create the account and the identity row in one transaction, pending when `approval`. `unique_id` is the ORCID if `subject_is_orcid`, otherwise an MFID.
4. Otherwise refuse and record it. Never match by email, username or name.

When a step-up sign-in names a `required_user`, these steps only look people up and never create anyone, so signing in with the wrong login leaves no stray account.

**Existing users get rows up front.** My first draft added rows at each person's next sign-in to avoid a production write, but the review showed that rule could re-arm after an unlink and leave an ORCID unbound. So the identity table becomes the only source of truth, in the standard order:

1. Ship the empty table. Every path that creates a user or signs one in also writes the row, while lookups still use `unique_id`. That includes `POST /users` with an ORCID and `admin create --orcid`.
2. Run the one-time script `migrations/data/20261103_backfill_user_identity.py`, idempotent, on staging and then production. New installations do not need it. It only inserts, and it covers every non-service user whose `unique_id` is an ORCID, with the configured ORCID issuer. This is a production data write and needs the user's approval.
3. Switch lookups to the table.

`POST /users` and linking check both `user_identity` and `unique_id`, so the two can never disagree about who holds an ORCID.

A second provider in CI (Dex) keeps the code from quietly depending on ORCID or on any one provider. A periodic job runs the same checks against Authentik.

## Registration

`CRUCIBLE_AUTH_REGISTRATION` sets the deployment's default, and each provider can override it. An administrator can change it at any time.

| Setting | First sign-in by someone new |
|---|---|
| `closed` (default) | Only an account an administrator created, or a login an administrator linked |
| `approval` | Anyone can register; the account waits until an administrator approves it |
| `open` | The account works immediately |

- The implicit ORCID provider defaults to `open`, which is today's behaviour at the Foundry and in the bundle's Hub setup.
- Password registration is allowed only with `approval`, because without email nothing verifies an address, and startup refuses `open` for passwords. The form takes a username, names and a password, under the same password rules, with the per-address limit.
- A person with a pending account sees that it is waiting only after proving who they are: a correct password or a valid provider sign-in. Otherwise pending looks like any other sign-in failure.
- Administrators see pending accounts and approve or reject them; rejecting deletes the account. Pending accounts not acted on go after 30 days, through the cleanup job.
- Rejecting and expiry use one function, `reject_pending_user`. In one transaction it locks the user, checks they are still pending, records the event, removes their identity group link and group, then deletes the user, which frees the username and login. If anything else still points at them, it returns 409 and changes nothing. It can never delete an approved user. Active users are never deleted, only disabled, because their samples and datasets point at them.
- A short list of restricted usernames lives in the code (`admin`, `root`, `system`, `support`, `security`, `crucible`, `hub`, `api`, `me`, fixed words under `/users/`, and so on), checked with the existing lookalike rule. It applies when someone picks their own name, at registration or rename. Administrators can still set one, so a lab's first administrator can be `admin`. Generated names avoid it, and existing holders keep theirs.
- What a new account can do once active is unchanged. The user reviewed this on 2026-11-02 and is content for now that any signed-in user can create projects and datasets and look up users.

## Linking and unlinking

Following the OWASP Authentication cheat sheet and the pre-hijacking research (Sudhodanan and Paverd, 2022):

- **A user's API key cannot change how that user signs in.** `require_recent_authentication` exempts API keys today (`app/utils/auth_utils.py:705`), so a leaked key could otherwise add a password, unlink ORCID and end the owner's sessions, turning a leaked key into a lost account. See API keys and credential routes below.
- **The link is written only after the Hub confirms it.** The flow, agreed with the user after the Phase 3 review:
  1. The Hub's "Link" action is a POST with CSRF protection, never a plain link, so another site cannot start it (django-allauth strongly recommends the same).
  2. The Hub's server calls the API with the user's session and gets a one-time start address. Nothing goes to the provider yet.
  3. The browser goes to that address on the API, which sets the login cookie there and redirects to the provider. A server-to-server call could not set this cookie, and RFC 9700 requires the one-time values to be bound to the browser.
  4. The provider returns to the API, which fully checks the token and writes nothing. It redirects to the Hub with a one-time code, as sign-in does.
  5. The Hub shows a page naming the account being linked. On confirm it calls `POST /auth/hub/link/confirm`, with its client secret in the `Authorization` header and the user's session token in the body, the shape RFC 7662 uses for token introspection.
  6. The API writes the row only if the code is valid and unused, PKCE matches, the session's user started this link, and the identity is free. So a victim opening an attacker's start address links nothing, and the reverse attack, attaching an attacker's login to a victim's account, fails as well.

  This keeps `HubLoginTransaction` and `HubLoginCode`, so the Hub does not move to Authlib's session, and parallel tabs and the step-up popup keep working. Keycloak's client-initiated linking starts in the browser the same way; ours adds the confirm step and session match.
- The provider's token is fully validated first.
- If that identity already belongs to someone else, or is someone else's `unique_id`, refuse. No merging in this phase. Accounts are never joined automatically.
- Unlinking is refused if it would leave no way to sign in, and ends the user's other Hub sessions.
- Every link and unlink writes an `auth_event`, and the Hub shows the list under the user's sign-in methods. Email notification waits until mail exists.
- Administrators can link on someone's behalf by exact issuer and subject, for moving a lab to a new provider, and it is recorded with their ID. Service accounts are refused.
- A person's ORCID is read only from `user_identity`, through `orcid_of`, with no fallback to `unique_id`. So an unlinked ORCID stops working for proposal sync, and the same person can link it back. Unlinking does not remove existing project memberships. This relies on the backfill having run before lookups switch. ORCID is the canonical researcher identifier when a person has one, but it is not the database key and not required.
- **ORCID's rules.** The Foundry is not an ORCID member, so ORCID's member requirements do not bind it, but Crucible follows them where it can, by the user's decision. An iD is collected by signing in. An administrator can still enter one, because the Foundry creates users from its user database, and it shows as unauthenticated, as ORCID's repository criteria allow. Crucible does not call ORCID's search API. Looking up an existing Crucible user by ORCID is not collecting an iD. The Hub shows iDs per ORCID's display guidelines, with the unauthenticated icon and suffix when `verified_at` is empty, and explains why it asks for one.
- **Verifying.** Signing in with ORCID sets `verified_at`. Someone with an entered ORCID and another way in uses "Verify your ORCID", which runs the linking flow: the same iD sets `verified_at`, and a different one is refused and changes nothing.
- Self-service proposal sync requires a verified ORCID, so a mistyped iD cannot grant someone another researcher's projects by their own action. `POST /users` keeps discovering projects from an entered ORCID, because the Foundry relies on it, and that is an administrator's logged decision: the row records them in `created_by_user_id`.
- Proposal sync (`POST /account/sync-projects`) stays where it is, with its code in `app/services/proposals.py`. No plugin system, since there is one source. A proposal service outage returns 502 from sync, while `POST /users` carries on without the lookup. The response's `orcid` comes from `orcid_of`, and sync and `POST /users` share one function that applies the stricter project checks.

## Local passwords

Sources: NIST SP 800-63B revision 4 sections 3.1.1.2 and 3.2.2; OWASP Password Storage, Authentication and Forgot Password cheat sheets. "SHALL" items from NIST are requirements here.

### Storage

- Argon2id through `pwdlib[argon2]` (0.3.1, August 2026), which wraps `argon2-cffi`. `pwdlib` is maintained but small, so it is pinned, and its only job is a thin wrapper over the PHC string. No custom hash format.
- Parameters above OWASP's floor of m=19 MiB, t=2, p=1, chosen by measurement on a small machine and stated in settings, with a test that fails if they fall below the floor. Salts come from the library.
- When parameters change, rehash at the next successful sign-in.
- Pepper: HMAC-SHA256 of the normalized password with a secret key held outside the database, base64-encoded, then Argon2id. This is the form OWASP accepts for pre-hashing, and NIST says SHOULD add a keyed step. It protects against a leak of the database alone, so its backup is kept apart from database backups, and the install guide says so, unlike the current advice to back up `secrets/` together with the database. `pepper_id` allows routine rotation at next sign-in. An empty pepper counts as missing, and startup refuses it when passwords are on. A stolen pepper needs everyone to reset, as OWASP notes. Losing it invalidates every password, but API keys keep working and an administrator can issue new links, so it is recoverable.

### Password rules

- At least 15 characters (NIST, single factor), counted in Unicode code points. Accept up to 256, which is above NIST's 64 and keeps every form within Caddy's 8 KB request limit once encoded.
- Apply NFC normalization before hashing (NIST SHOULD). Never trim, truncate or alter it otherwise.
- No composition rules and no scheduled expiry (NIST SHALL NOT). A change is forced when there is evidence of compromise (NIST SHALL): an administrator removes the password and issues a link, and `auth revoke-passwords` does it for everyone after a breach.
- Compare the whole password, case-insensitively, against a list shipped in the image, plus context values: the username, email, names and the service name. Say why it was rejected, and give short guidance on choosing a good one (NIST SHALL). Since few common passwords reach 15 characters, the list is built from a licensed public source and filtered to that length. No online check, so it works offline.
- Password managers, autofill and paste work: correct `autocomplete` values, no blocking script, and a show-password control.
- No hints or security questions.
- An empty submitted password or a missing stored hash never verifies.

### Attempts

- Counted on the account. Each attempt first counts itself and checks the limits in one atomic `UPDATE ... RETURNING`, then hashes, so parallel guesses cannot slip past the counter, no database lock or connection is held during hashing, and a crash counts as a failure. A success is accepted only if `password_changed_at` has not changed since the attempt began, so a guess at an old password cannot succeed during a reset.
- For unknown browsers, the first 4 failures have no delay, then 2, 4, 8 seconds and so on, up to 15 minutes. A success resets both counters.
- During a delay, the same work is done against a dummy hash, the submitted password is not checked, and the attempt does not count. So the response and timing match a wrong password.
- Two counters, decided by the user after the Phase 3 review, combining NIST and OWASP. Unknown browsers have a limit of 90, after which only they are locked out. A known browser or a signed-in session has its own limit of 10 and no delays, after which the password is disabled until an administrator issues a new link, the rebinding NIST requires. The total can never pass NIST's 100. The 90 and 10 split is this project's design, not from either source.
- Attackers cannot reach the second counter without a valid cookie or session, so they can delay a person but not lock them out of a browser they have used. A correct password from a known browser clears the lockout on unknown browsers.
- A browser that signs in successfully gets a signed cookie (OWASP's device cookie), signed with `itsdangerous` under its own salt, so no other signed value can pass as one. Each sign-in issues a new one. It carries `password_changed_at`, so any password change or reset cancels all cookies. Failures are counted per account, not per cookie as OWASP does, which needs no extra table and stops a stolen cookie adding guesses.
- What this does not solve: someone with no known browser can be locked out by a persistent attacker in about 20 hours of guessing, and then needs a known browser or an administrator's link. Closing that needs email or a second factor, out of scope here.
- Per address: a wider limit, to slow one source trying many accounts, also covering registration and opening or submitting password links. The address comes from the header the bundle's Caddy sets, trusted only when configured.
- `POST /account/password` uses the known-browser counter, so a stolen session cannot guess the current password freely, and an attacker's guesses cannot block a signed-in person from changing it.
- A limit on simultaneous hash checks in each worker, so a burst cannot exhaust memory.

### Responses

- One generic error for unknown user, wrong password, delayed, locked, disabled, or no password set, worded so a delay also makes sense ("Sign-in failed. Check your details, or try again in a few minutes."). The same work happens in each case, and a test compares timings.
- The sign-in name is the username, which is lowercase and unique. Email is not used: users can edit it, and nothing verifies it yet. The rename rules are under Changes to existing tables.

### The form

- Served by the API under `/auth/hub/`, over HTTPS whenever it crosses a network. Startup refuses passwords on when the public address is plain HTTP on anything but `localhost`, the same rule as `_trusted_public_url`.
- POST only, tied to the login record: the state value plus the browser cookie, which is also the CSRF protection. A failed attempt does not use up the record, unlike `consume_login` (`hub_login.py:133`), so a typo does not restart sign-in. It is consumed on success.
- Headers: `Cache-Control: no-store`, a strict Content-Security-Policy with `frame-ancestors 'none'`, and `Referrer-Policy: no-referrer`.
- A password must never reach logs. A test submits a marker password through each failure path and asserts it appears in no log or response.

### Setup, change and reset

- An administrator creates a user, then gets a one-time password link valid for 24 hours by default, and passes it on however they like. The person sets a password, and can add a provider afterwards through normal linking. No temporary password exists.
- Invites do not attach a provider login (user's decision after the Phase 3 review, 2026-11-04). The other paths cover it: create the account with a known ORCID, link an exact issuer and subject, or set the provider to `approval` for people whose ID an administrator cannot know. This can be added later without changing `password_token`.
- The form is a POST with CSRF protection, and using a link locks its row, so two tabs cannot both succeed.
- An administrator who knows someone's ORCID can instead create the account with it, which writes the identity row, and no link is needed.
- Opening the link never consumes it, because mail scanners follow links; only submitting the form does. On first open the token moves into a short-lived cookie and the browser is redirected to an address without it, so the token does not linger in proxy logs or history.
- `python -m app.cli admin create` prints a link as well as the API key when passwords are on, and `admin password-link` issues one, so a locked-out sole administrator can recover from the server. No default password and no open setup page.
- Using a link writes the password, consumes the link, clears any lock, ends all of that user's Hub sessions, refuses handoff codes issued before it, and sends them to the normal sign-in (OWASP: do not sign in automatically).
- Changing a password needs the current password and a recent sign-in, and ends the user's other Hub sessions.
- A user with no password who wants one (an ORCID user, say) needs a recent sign-in, then sets it.
- Password events and API key events are separate. A password change or reset never touches API keys, and revoking keys is its own action, through the existing `DELETE /users/{user_ref}/apikey`. An administrator handling a compromise does both.

## API keys and credential routes

| Action | With an API key (CLI) | Hub session |
|---|---|---|
| Issue a setup or reset link, approve, disable, enable | Admin key works, and is logged | Admin, recent sign-in |
| The same, on another human administrator's account | Refused | Admin, recent sign-in |
| Change any human's admin status, promoting or demoting | Refused | Admin, recent sign-in |
| Regenerate or read another administrator's API key | Refused | Admin, recent sign-in |
| Link or unlink another administrator's login | Refused | Admin, recent sign-in |
| Revoke someone's API keys | Admin key works, on anyone, as today | Admin |
| Change own password | Key plus the current password, on the same attempt counter | Recent sign-in plus the current password |
| Set a first password | Refused; the CLI prints a browser link | Recent sign-in |
| Link or unlink a login | Refused; the CLI prints a browser link | Recent sign-in |

Admin keys may issue reset links for ordinary users, for the CLI. For human administrators, the rule is broader, decided by the user after the Phase 3 review: the earlier rule could be bypassed by demoting the administrator first or regenerating their key. No rule makes a leaked admin key safe, since it can read and change all data and can make a service account an administrator (nano-crucible's `set_platform_role` relies on that route). The goal is that other human administrators keep control of their own accounts, so someone can respond. A lab with no Hub uses the server command line: `python -m app.cli admin set-role` and `admin password-link`. Revoking keys stays open, since it removes access rather than granting it, and nano-crucible already relies on it. Later, sign-in from a terminal (browser or device code, RFC 8628) and scoped keys can narrow what an admin key must do.

## API routes

All are listed in the OpenAPI snapshot for client review.

- User: `GET /account/identities`, `POST /account/identities/{provider}/link` (returns the start address), `GET /auth/hub/link/start` (browser, sets the cookie), `POST /auth/hub/link/confirm`, `DELETE /account/identities/{id}`, `POST /account/password`.
- Admin: `POST /users/{user}/password-link`, `DELETE /users/{user}/password`, `POST /users/{user}/approve`, `POST /users/{user}/reject`, `POST /users/{user}/disable`, `POST /users/{user}/enable`, `POST /users/{user}/identities`, `DELETE /users/{user}/identities/{id}`, `GET /users/{user}/auth-events`.
- Public, for signing in: `GET /auth/methods`, `GET` and `POST /auth/hub/password`, `GET` and `POST /auth/hub/password-link`, `GET` and `POST /auth/hub/register`, `GET /auth/hub/oidc/{key}/callback`.

## API keys

These sit beside passwords, so plain text keys undo much of this work. The target shape, from Tiled, Gitea and JupyterHub:

| Column | Notes |
|---|---|
| `id` | primary key |
| `user_id` | not null, FK `user.id` |
| `key_hash` | unique; SHA-256 is enough because keys are 256-bit random values (`secrets.token_hex(32)` today) |
| `key_prefix` | first characters, to identify a key in lists and logs |
| `name` | what it is for |
| `created_at`, `last_used_at`, `expires_at`, `revoked_at` | expiry is optional |

Several keys per user, shown once, through `POST`, `GET` and `DELETE /account/apikeys`. Existing keys keep working because their hashes can be computed from the stored values, then the plain column is dropped. This is a production data change and goes through the usual approval. Clients first: nano-crucible still reads `GET /account/apikey`, and tells people to fetch a key from `/user_apikey`; `/auth/apikey` and `/user_apikey` must become pages that add a new key and never replace one, and `GET /account/apikey` answers 404 with instructions. The nano-crucible and Hub sessions confirm before anything is removed.

## Order of work

Each is one or more small PRs to `main`, off by default until the last step. Merges reach staging; nothing is released to production until all of them are ready, as the user decided on 2026-11-03.

1. **Older ORCID flow onto Authlib.** Move `/auth/login` and `/auth/callback` onto the shared code, which adds `state` and a real `nonce`. Needed regardless.
2. **Confirm with clients.** Ask the Hub session whether it handles MFID users and checks that a re-login returned the same person; ask the nano-crucible session what it relies on. Their answers may add work.
3. **Identity table and one resolver.** Empty table, double writes, backfill with approval, then lookups switch, while ORCID is still the only provider.
4. **Generic OIDC.** Provider list, discovery, callbacks, changed login tables, chooser page, Dex in CI. Foundry behaviour is checked unchanged on staging with crucible-tests.
5. **Linking and registration.** User and admin routes, `disabled_at`, `pending_at`, the registration setting, then the Hub's sign-in methods page through the Hub session.
6. **API key hashing.** Clients first. Must be finished before password sign-in is on by default anywhere.
7. **Local passwords.** Everything in that section, behind `CRUCIBLE_AUTH_PASSWORD_ENABLED`, off by default in code.
8. **Independent security review** of steps 1 to 7 before release, with the test list below, checking each requirement here separately. A professional review is worth considering if labs outside LBL will run it.
9. **Compose default.** Passwords on in the bundle, `admin create` prints a link, cleanup job added, install guide rewritten around HTTPS, and a release.

Changes to staging and production databases need the user's approval, as always; steps 3, 4, 5 and 6 involve them.

## Tests that must exist

Settings: zero or negative link, cookie and retention lifetimes refused; an empty pepper refused; passwords on a plain HTTP public address refused.

Password: floor on hash parameters; NFC round trip; 15 and 256 boundaries; blocklist reason; parallel guesses cannot exceed the counter; delay and lock schedule; delayed attempts not counted; device cookie bypass, and its own limit; lock leaves other methods working; only a new link clears a lock; rename keeps the lock and does not pass it on; timing comparison across all failure cases; marker password in no log or response; rehash on parameter change; pepper rotation; service accounts refused.

Links: single use; expiry; opening does not consume; one live link under concurrent requests; voided by a newer link, disable and password change; hash only in the database; no automatic sign-in; sessions ended; a handoff code from before the reset refused.

OIDC: wrong `iss`, `aud`, `nonce`, expired and unknown key; `none` and HS256 refused; callback on the wrong provider refused; `state` from another browser refused; missing `auth_time` not recent; `subject_is_orcid` refused on another issuer; ORCID-shaped `sub` from another provider gets no ORCID treatment; a typo in the password form keeps the login record.

Linking: identity already taken, by a row or by `unique_id`; no email matching even when emails are equal; an attacker's link address opened by a victim writes nothing; API keys refused on credential routes; last method cannot be unlinked; an unlinked ORCID cannot sign back in, including after the password is also removed.

Disable: every method, handoff codes, Hub refresh, Hub access tokens and API keys are refused.

Registration: each setting and provider override; `open` for passwords refused at startup; a pending account gets no session or key on any path; the waiting message appears only after proof of identity; password links work with `closed`; pending accounts expire. Credential routes: every row of the table above.

Foundry: existing ORCID user signs in, keeps `unique_id`, and `/auth/apikey` still works, before and after the backfill.

## HTTPS on a lab network

This remains the hardest part of a local install, whichever sign-in is used: passwords must not cross a network unencrypted, and the Hub's cookies need HTTPS. The install guide's default is a real domain name with Let's Encrypt, with a DNS challenge for names only reachable inside a lab, and a clear note on the single-machine option using Caddy's own certificate. A lab that has none of these can still use the API and nano-crucible with API keys on one machine.

## Not in this phase

Email reset, multi-factor, passkeys, account merging, and SAML inside Crucible. A group needing these sooner uses Authentik or Keycloak through OIDC, or CILogon for institutional login. The tables leave room for them: email reset reuses `password_token`, and a future factor gets its own table keyed by `user.id`.

## Decisions

Decided by the user on 2026-11-02:

1. Existing ORCID users get identity rows up front. An insert-only production write, tested on staging first, run only with the user's go-ahead at that step.
2. A pepper is on by default, kept apart from database backups.
3. A password locks at 100 failures, cleared only by a new link.
4. Password resets and API key resets are separate events.
5. API keys are hashed before passwords ship.

6. Registration is one setting, `closed`, `approval` or `open`, with provider overrides, replacing the `create_accounts` flag.
7. API keys follow the table under API keys and credential routes: administrators can use keys from the CLI, and a user's own key cannot change how they sign in without their current password.
8. New accounts' default permissions stay as they are for now.
9. One-time links replace temporary passwords, and `user.disabled_at` is added.
10. From the Hub's standards review on 2026-11-03: an enforced `required_user` replaces the planned `login_hint`, the token response gains `auth_time`, sign-in failures reach the Hub as standard OAuth errors, and API key creation and revocation are logged.
11. No multi-factor sign-in in this phase, knowing that a password account can get an API key with only a recent password sign-in. Groups that need it use a provider.
12. An API key can still regenerate itself for now. Revisit when keys gain scopes.
13. For this phase, the Hub stays a backend-for-frontend, the pattern RFC 10017 ranks most secure, and talks to the API through first-party OAuth 2.0 with PKCE, documented as that and not as OIDC. This is the current state, not a rule: the goal is to unify the Hub-to-API link and upstream sign-in on OIDC when that becomes practical, for example if a maintained OIDC provider library supports FastAPI, or an outside app needs one. New work should not make that harder. Authlib's provider support currently covers only Flask and Django.

## Left to the builder

Exact Argon2id parameters above the floor, the source and size of the blocklist, the per-address limit, the device cookie's lifetime and limit, settings names, and how the bundle gives the API a way to reach a provider at its public address, which the Authentik test showed matters because its issuer follows the address used to reach it.

## Sources

- [NIST SP 800-63B-4](https://pages.nist.gov/800-63-4/sp800-63b.html)
- [OWASP Password Storage](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html), [Authentication](https://cheatsheetseries.owasp.org/cheatsheets/Authentication_Cheat_Sheet.html) and [Forgot Password](https://cheatsheetseries.owasp.org/cheatsheets/Forgot_Password_Cheat_Sheet.html) cheat sheets
- [OWASP device cookies](https://owasp.org/www-community/Slow_Down_Online_Guessing_Attacks_with_Device_Cookies)
- [OpenID Connect Core 1.0](https://openid.net/specs/openid-connect-core-1_0.html)
- [RFC 9700, OAuth 2.0 Security Best Current Practice](https://www.rfc-editor.org/rfc/rfc9700.html)
- [Pre-hijacked accounts (Sudhodanan and Paverd, 2022)](https://arxiv.org/pdf/2205.10174)
- [ORCID discovery document](https://orcid.org/.well-known/openid-configuration)
- Comparison sources are in the logbook entry of 2026-11-02 and in the Phase 3 discussion.
