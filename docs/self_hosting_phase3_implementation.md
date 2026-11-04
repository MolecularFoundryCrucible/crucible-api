# Self-hosting Phase 3: sign-in implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: written 2026-11-02 for review; not started. The Hub and nano-crucible answers are recorded in Task 2 and applied to the tasks below.

**Goal:** Crucible speaks OIDC to any number of providers and has its own password sign-in, with one person able to link several logins to one account, while the Foundry keeps signing in with ORCID unchanged.

**Architecture:** A new `app/auth/` package holds the Authlib registry, and becomes the one home for sign-in code, with routes kept thin and the `hub_*` files and `auth_utils.py` moved in as tasks touch them. It also holds the single sign-in resolver, passwords, links, throttling and the event log. Every sign-in path ends in the resolver and then the existing Hub handoff. New tables reference `user.id`; `user.unique_id` never changes.

**Tech Stack:** Python 3.11, FastAPI, SQLModel and Alembic on PostgreSQL 17, Authlib's FastAPI integration (`authlib.integrations.starlette_client.OAuth`, with `SessionMiddleware`, `httpx` and `itsdangerous`), joserfc, `pwdlib[argon2]` (new, pinned), Dex in CI, Caddy, Docker Compose.

**Spec:** `docs/self_hosting_phase3_plan.md` (approved decisions; read it, it holds the table definitions and the reasons). Background: `docs/self_hosting_design.md`, `docs/self_hosting_secrets.md`, `docs/hub_auth_operations.md`.

## Global Constraints

- The Foundry signs in unchanged at every step: same ORCID client, same registered redirect addresses (`{api}/auth` for the older flow, `{api}/auth/hub/callback` for the Hub), same `unique_id` values. crucible-tests runs on staging before each merge that touches sign-in.
- Password sign-in is `CRUCIBLE_AUTH_PASSWORD_ENABLED`, off by default in code, until Task 15.
- `CRUCIBLE_AUTH_REGISTRATION` defaults to `closed`; the implicit ORCID provider defaults to `open`; `open` for passwords is refused at startup.
- Match identities only on exact `issuer` plus `subject`. Never on email, username, name, or what a value looks like. ORCID status comes only from `subject_is_orcid`, allowed only for `https://orcid.org` and `https://sandbox.orcid.org`.
- Argon2id floor m=19 MiB, t=2, p=1. Passwords 15 to 256 Unicode code points, NFC-normalized, never trimmed or truncated.
- Delays: failures 1 to 4 none, then 2, 4, 8 seconds and so on, up to 900 seconds. Hard lock at 100 counted failures. `STEP_UP_MAX_AGE` stays 600. Link lifetime 24 hours by default.
- No migration or data write on staging or production without the user's explicit approval at that moment. Tasks 3, 4, 5, 6, 8 and 9 include migrations; Tasks 3 and 8 also change data.
- Each task is one or more pull requests to `main`, opened by the agent and merged by the user, never pushed directly. Client changes go through the nano-crucible and crucible-hub sessions, not edits from here.
- PR bodies are a concise summary of one-line bullets with no test plan. `CHANGELOG.md` entries are one line under `Unreleased`. Markdown is not hard-wrapped. No em dashes. No code comments unless the why is non-obvious.
- Tests run with `uv run pytest -q`. Concurrency tests use the real Postgres fixture (`HUB_AUTH_TEST_DATABASE_URL`, see `tests/test_hub_auth_postgres.py`). Automated tests never create users on staging.
- `crucible` is nano-crucible's command; server commands stay under `python -m app.cli`.

## Review Focus

1. A provider's discovery document is unreachable when the API starts: the API still starts, other methods work, that provider reports unavailable and recovers by itself. Task 4 tests this; it matters most for the Foundry's ORCID.
2. A client sending its own `X-Forwarded-For`: it must not change which address is throttled, or anyone could escape the limit. Task 11 tests this.
3. A sign-in form left open longer than the 10 minute login lifetime: submitting it says the sign-in expired and offers a restart, not "sign-in failed", and does not count against the account. Task 12 tests this.
4. A provider removed from settings while users have only that login: the settings check names how many are affected, and an administrator can issue them password links. Task 4 tests the check.
5. A username typed with capitals or stray spaces: `Ada` finds `ada`, and surrounding spaces in the username are removed, while the password is never altered. Task 12 tests this.

---

## Order

The user decided on 2026-11-03 that nothing is released until all of Phase 3 is ready: merges reach staging, and production stays on its release. Each task is proposed to the user first and built only after they agree. Tasks 3 to 8 build the provider side while ORCID stays the only provider at the Foundry. Task 9 (API keys) must finish before Task 15 turns passwords on anywhere by default, as the user decided. Tasks 10 to 13 are passwords behind the off switch. Task 14 is the review gate.

## File map

| File | Responsibility | From |
|---|---|---|
| `app/auth/providers.py` | Authlib registry, issuer and algorithm checks; provider list from settings | Task 1, Task 4 |
| `app/auth/browser_login.py` | the older browser flow | Task 1 |
| `app/auth/events.py` | `record_event` | Task 3 |
| `app/auth/identity.py` | the resolver, ORCID helper | Task 3 |
| `app/auth/linking.py` | link, unlink | Task 6 |
| `app/auth/registration.py` | registration setting, approve, reject | Task 7 |
| `app/auth/passwords.py` | hash, verify, rules | Task 10 |
| `app/auth/throttle.py` | account counters, address limit, device cookie | Task 11 |
| `app/auth/password_links.py` | setup and reset links | Task 13 |
| `app/api/routes/hub_auth.py` | sign-in routes, callbacks, chooser, form | modified throughout |
| `app/api/routes/identities.py` | `/account/identities`, admin identity routes | Task 6 |
| `app/api/routes/passwords.py` | `/account/password`, admin password routes | Task 13 |
| `app/cli/auth.py` | `auth` command group | Task 13 |
| `migrations/data/20261103_backfill_user_identity.py` | one-time identity backfill | Task 3 |

---

## Task 1: Older ORCID flow onto Authlib

Done in PR #411. After review the user asked for routes without ORCID specifics, and for established tools: a first version wrapping Authlib's low-level parts was replaced by its FastAPI integration, which provides `state`, `nonce`, discovery, signing key refetch and ID token checks. `app/auth/providers.py` adds what Authlib does not: the discovered issuer must match the configured one exactly, and only asymmetric signing algorithms are accepted. The Hub flow moves onto the same registry in Task 4. Checked against a fake ORCID in tests, a real Dex server, and ORCID's real discovery document; after merge, the user signs in through `https://crucible.lbl.gov/testapi-staging/user_apikey`.

## Task 2: Client answers and remaining check

The Hub (at 3f63bfc, v1.7.2) and nano-crucible sessions answered on 2026-11-02, from reading their code; nano-crucible also ran an offline mocked probe. Nothing has been changed on their side, and their changes need the user's approval.

**Hub:**
- MFIDs should work. IDs are opaque strings, and no orcid.org link is built from one. Only labels mention ORCID.
- **A re-login has no same-user check.** `finishHubLogin` replaces the session with whoever came back, and the dialog then says "Signed in again", so a retried action runs as the other account. Several sign-in methods make this easier to hit. A Hub fix is needed before Task 4 ships (crucible-hub PR #11, awaiting merge); Task 4 also adds a check on the API side.
- Keep unchanged: `/auth/hub/login`, `/token`, `/refresh`, `/revoke`, the token response shape, including `user_unique_id`, and the exact token `insufficient_user_authentication`.
- Linking cannot reuse `/hub/auth/callback`, because any valid return replaces the session. It needs its own path, `/hub/auth/link/callback`, also in the API's allowlist, returning by top-level GET. The confirmation page lives under `/hub/account/`.
- The chooser and form must work at 520x720 in a popup, full page, never framed. With `max_age=0` the chosen method must force fresh sign-in. The Hub's transaction cookie lasts 10 minutes, matching the API's.
- The Hub never reads an existing API key, so show-once needs no Hub change.

**nano-crucible:**
- `GET /account/apikey` and `account api-key` are in every release since v3.0.0. Once keys are hashed, the route must answer 404 with create-a-key instructions in `detail`; a 200 without `api_key` raises KeyError.
- `/user_apikey` must stay, as a page that adds a key and never replaces one, or following setup on a second machine would break the first.
- A wrong current password must not be 401, which the client reports as "Your API key was not accepted". New refusals need a stable `code`, because every 403 otherwise suggests `-E/--elevate`.
- v3.0 and v3.1 cannot look up a user by MFID, so a provider-created user with no username could be referred to only by email.
- `project add-user --user <MFID>` fails in every release (`cli/project.py:663`). This is a client bug, and it needs the user's approval to fix.
- The client will use no retry and no body logging on the new credential routes, and it needs the key route shapes (Task 9).

**Deployment (2026-11-03):** staging and production both use issuer `https://orcid.org`, so the backfill is the same for both. They use two ORCID apps: `HUB_AUTH_ORCID_CLIENT_ID` for the Hub and `ORCID_CLIENT_ID` for the older flow. ORCID iDs do not depend on the app, so identities are unaffected, but Task 4 keeps two registered clients, because each app has its own registered redirect address.

- [ ] **Step 1: The user approved both fixes on 2026-11-03,** the Hub session makes the same-user fix, and the nano-crucible session fixes `cli/project.py:663`.

## Task 3: Identity table and double writes

Empty tables, no change in behaviour. Every path that creates or signs in an ORCID user also writes the identity row.

**Files:**
- Create: `app/auth/identity.py`, `migrations/data/20261103_backfill_user_identity.py`, a migration
- Modify: `app/models/core_models.py`, `app/cli/admin.py`, `app/api/routes/users.py:535-615`, `app/api/routes/auth.py:117-145`, `app/utils/hub_login.py:140-173`
- Test: `tests/test_auth_identity.py`, `tests/test_auth_identity_migration.py`

**Interfaces:**
- Produces: model `UserIdentity`, columns as in the spec.
- `ensure_identity(session, user: User, issuer: str, subject: str, *, label: str | None = None, created_by: User | None = None) -> UserIdentity`, idempotent, raising `IdentityTaken` if another user holds it.

- [ ] **Step 1: Write failing tests:**
  - `test_identity_is_unique_on_issuer_and_subject`
  - `test_second_orcid_for_one_user_is_refused` (partial unique index, Postgres fixture)
  - `test_two_identities_on_a_non_orcid_issuer_are_allowed`
  - `test_ensure_identity_is_idempotent`, `test_ensure_identity_raises_when_held_by_another_user`
  - `test_hub_sign_in_writes_identity_row`, `test_apikey_page_writes_identity_row`, `test_post_users_with_orcid_writes_identity_row`, `test_admin_create_with_orcid_writes_identity_row`
  - `test_service_account_gets_no_identity_row`
  - `test_backfill_inserts_only_orcid_humans`, `test_backfill_is_idempotent`, `test_backfill_dry_run_writes_nothing`, `test_backfill_requires_issuer_flag`
  - `test_migration_adds_empty_tables_and_downgrades`
- [ ] **Step 2: Run to verify they fail.** `uv run pytest -q tests/test_auth_identity.py`.
- [ ] **Step 3: Add models and migration.** `user_id` foreign keys use `ondelete="CASCADE"`. Partial unique index: `ux_user_identity_one_orcid` on `user_id` where `issuer IN ('https://orcid.org', 'https://sandbox.orcid.org')`.
- [ ] **Step 4: Implement helpers and double writes.** Each writer calls `ensure_identity` with the issuer of the flow it belongs to, in its existing transaction; `provision_user` covers `POST /users` and `admin create --orcid`. Lookups still use `unique_id`.
- [ ] **Step 5: Implement the one-time script `migrations/data/20261103_backfill_user_identity.py --issuer URL [--commit]`,** following `migrations/README.md`: dry run by default, insert-only, batches of 1000, printing counts. New installations do not need it.
- [ ] **Step 6: Run the full suite, commit, PR to `main`.**
- [ ] **Step 7: With the user's approval at that moment,** run the migration on staging before merging, because sign-in writes to the new table. After merging, run the backfill on staging with `--issuer https://orcid.org`. Expected: row count equals non-service users whose `unique_id` is an ORCID. Production follows at the release, in the order under Task 5.

## Task 4: Generic OIDC

From the review: while a provider's discovery document is bad, each `/auth/login` fetches it again with no backoff. Add a short pause between retries when generalizing the provider code.

**Files:**
- Create: `app/auth/providers.py`, a migration, `tests/fixtures/dex.yaml`
- Modify: `app/auth/oidc.py`, `app/config.py` (new `AuthSettings` group), `app/utils/hub_login.py:21,63-72,140-173`, `app/api/routes/hub_auth.py`, `app/api/main.py:65-70`, `app/cli/settings.py`, `deploy/compose/Caddyfile:13-23`, `.github/workflows/tests.yml`, `docs/hub_auth_operations.md`
- Remove: `app/utils/hub_orcid.py` wrappers once unused
- Test: `tests/test_auth_providers.py`, `tests/test_auth_oidc_integration.py`, existing Hub tests

**Interfaces:**
- `AuthSettings` (env prefix `CRUCIBLE_AUTH_`): `password_enabled: bool = False`, `registration: Literal["closed", "approval", "open"] = "closed"`, `providers: str | None` (JSON list; secrets read as `OIDC_{KEY}_CLIENT_SECRET` through the existing secret helpers).
- `@dataclass(frozen=True) class Provider: key: str; display_name: str; client: OidcClient | None; issuer: str; registration: str; subject_is_orcid: bool; legacy_callback: bool`
- `get_providers() -> dict[str, Provider]`. With no list and `hub.enabled`, returns one implicit provider: key `orcid`, `registration="open"`, `subject_is_orcid=True`, `legacy_callback=True`, client ID from `hub.orcid_client_id or api.orcid_client_id`.
- Each provider is registered on the Authlib registry with `server_metadata_url`, and `code_challenge_method="S256"` when advertised. Discovery is lazy, so an unreachable provider does not stop startup.
- The Hub flow's provider leg moves onto the registry. `hub_orcid.py` is removed; its API-to-Hub transaction table stays.
- The older flow keeps its own registered client, `ORCID_CLIENT_ID`, with its own redirect address.
- `hub_login.create_login(..., provider_key: str, purpose: str = "sign_in", user_id: int | None = None)`.
- `GET /auth/hub/login` gains optional `required_user` (a `unique_id`). If a different account authenticates, no code is issued and the API redirects to the Hub with `error=login_required` and `state` (OIDC Core 3.1.2.1 leaves `login_hint` to the server's discretion, so an enforced rule needs its own name). Old Hubs omit it and behave as today. It passes through the browser unsigned, so it is a second check, and the Hub's own comparison remains the enforcing one.
- `POST /auth/hub/token` adds nullable `auth_time`; current Hubs ignore unknown fields, so no Hub change has to go first.
- After a valid login record exists, failures redirect to the Hub with `state` and RFC 6749 4.1.2.1 errors: `access_denied`, `login_required`, `temporarily_unavailable`, `server_error`. An invalid or expired record gets the API's own page, because its redirect target cannot be trusted.
- `GET /auth/methods` returns `[{key, display_name, kind}]`, so the Hub can stop saying ORCID.
- `resolve_sign_in(session, provider: Provider, identity: VerifiedIdentity) -> SignInOutcome` lands in Task 5; here the flow still calls `ensure_identity` for ORCID only.
- The chooser and every sign-in page fit a 520x720 window and send `frame-ancestors 'none'`.

- [ ] **Step 1: Write failing tests:**
  - Settings: `test_no_list_gives_implicit_orcid_provider`, `test_implicit_uses_orcid_client_id_fallback`, `test_subject_is_orcid_refused_on_other_issuer`, `test_issuer_must_be_https_or_localhost`, `test_invalid_provider_json_fails_settings_check`, `test_foundry_environment_snapshot_produces_same_provider` (values from Task 2)
  - Discovery: `test_issuer_mismatch_marks_provider_unavailable`, `test_unreachable_discovery_does_not_stop_startup`, `test_other_providers_work_while_one_is_down`, `test_provider_recovers_after_discovery_returns`, `test_trailing_slash_in_issuer_is_compared_exactly`
  - Tokens: `test_unknown_kid_refetches_keys_once`, `test_algorithm_not_advertised_refused`, `test_none_and_hs256_always_refused`
  - Flow: `test_one_method_redirects_directly`, `test_several_methods_show_chooser`, `test_chooser_escapes_display_name`, `test_callback_for_other_provider_refused`, `test_state_from_other_browser_refused`, `test_legacy_callback_only_serves_implicit_orcid`, `test_pkce_sent_when_advertised`, `test_pkce_omitted_for_orcid`, `test_mfid_user_can_finish_handoff`
  - Recent sign-in: `test_step_up_sends_prompt_login_with_max_age`, `test_missing_auth_time_is_not_recent`, `test_required_user_mismatch_issues_no_code_and_returns_login_required`, `test_token_response_includes_auth_time`, `test_upstream_failures_redirect_with_standard_errors`, `test_invalid_login_record_never_redirects`, `test_chooser_with_max_age_zero_forces_fresh_sign_in_for_each_method`, `test_challenge_token_is_exactly_insufficient_user_authentication`
  - Contract: `test_token_response_shape_unchanged`
  - Check: `test_settings_check_names_users_stranded_by_removed_provider`
  - Integration, against Dex in CI: full code flow, same `sub` on a second sign-in, and a second issuer's identical `sub` kept separate
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Migration** adds `purpose` (default `sign_in`), `provider_key`, and nullable `user_id` (FK `user.id`) to `hublogintransaction`, and `purpose` plus nullable `issuer`, `subject`, `label` to `hublogincode`. Additive; old pods keep working during rollout.
- [ ] **Step 4: Implement settings, discovery, provider list and flow.** New callback route `GET /auth/hub/oidc/{key}/callback`, sharing the handler with `/auth/hub/callback`. Remove the `ORCID_PATTERN` check at `hub_login.py:141` and the issuer allowlist at `hub_login.py:68`. `_is_hub_auth_request` and the Caddy block already cover `/auth/hub/*`, so all new sign-in routes stay under that prefix; add nothing outside it except `GET /auth/methods`.
- [ ] **Step 5: Add Dex to the CI job** using the image version pinned in the workflow, static password users, one client.
- [ ] **Step 6: Run the full suite, commit, PR.** Staging check with crucible-tests after merge, and the user signs in to the Hub once. Expected: same `unique_id`.
- [ ] **Step 7: Add a periodic CI job** running the integration tests against Authentik with the blueprint from the 2026-11-02 trial (`server` and `worker` without the Docker socket).

## Task 5: One resolver, lookups switch, disable and pending

All sign-in paths use `resolve_sign_in`, and the event log arrives here, moved from Task 3 on the user's decision. Lookups switch behind `CRUCIBLE_AUTH_IDENTITY_LOOKUP`, off by default, so one release can go out safely: migrate, deploy (rows start being written), backfill, then turn the setting on. Off is the rollback. The flag is temporary and is removed in a later release, as the user decided on 2026-11-03.

**Files:**
- Create: `app/auth/events.py`
- Modify: `app/auth/identity.py`, `app/models/core_models.py`, a migration, `app/api/routes/auth.py`, `app/utils/hub_login.py`, `app/utils/hub_tokens.py:174-182`, `app/utils/auth_utils.py`, `app/utils/user_utils.py:207,297`, `app/api/routes/account.py:127`, `app/api/routes/users.py:441,554`
- Test: `tests/test_auth_resolver.py`, `tests/test_auth_disable.py`

**Interfaces:**
- `@dataclass class SignInOutcome: user: User | None; status: Literal["ok", "pending", "refused"]; reason: str | None`
- `resolve_sign_in(session, provider: Provider, identity: VerifiedIdentity) -> SignInOutcome`, following the spec's "Who is this person" order, steps 1 to 4.
- Model `AuthEvent` as in the spec, indexed on (`user_id`, `at`). `record_event(session, event: AuthEventType, *, user: User | None = None, actor: User | None = None, issuer: str | None = None, client_address: str | None = None) -> None`, in the caller's transaction, also logging. `AuthEventType` is a `str` enum.
- `orcid_of(session, user: User) -> str | None`, moved from Task 3 because nothing uses it before this task: the `user_identity` subject on an ORCID issuer, else `unique_id` if `is_orcid`.
- `assert_active(user: User) -> None`, raising the existing generic failure when `disabled_at` or `pending_at` is set.
- One function creates a person, replacing the separate code in `/auth/apikey` and `create_handoff_code` and shared with `POST /users` and `admin create` (user's decision, 2026-11-04). Every person it creates has a non-empty username, generated with `generate_unique_username` when none is given. Before any `NOT NULL` constraint, count existing users with no username and bring the numbers to the user.
- `/auth/apikey` and the Hub find people through `user_identity`, so someone whose `unique_id` is an MFID and who has a linked ORCID gets in instead of being refused.
- The `is_orcid(unique_id)` checks at `account.py:127`, `users.py:554` and `user_utils.py:142` read the ORCID from `user_identity`.

- [ ] **Step 1: Write failing tests:**
  - Resolver: `test_known_identity_signs_in`, `test_unlinked_orcid_is_refused_with_message`, `test_unlinked_orcid_stays_refused_after_password_removed`, `test_no_match_by_email_when_emails_equal`, `test_orcid_shaped_sub_from_other_provider_gets_mfid`, `test_new_orcid_user_unique_id_is_the_orcid`, `test_new_other_provider_user_unique_id_is_mfid`, `test_new_mfid_user_gets_generated_username`, `test_concurrent_first_sign_in_creates_one_user` (Postgres), `test_identity_write_rolls_back_with_user`
  - Entry points, parametrized over the Hub callback and `/auth/apikey`: `test_all_entry_points_refuse_disabled`, `test_all_entry_points_refuse_pending`, `test_all_entry_points_use_resolver`
  - Disable: `test_disabled_refused_on_handoff_redemption`, `..._on_hub_refresh`, `..._on_hub_access_token`, `..._on_api_key_after_cache_clear`
  - `test_foundry_user_signs_in_and_keeps_unique_id_after_backfill`, `test_flag_off_keeps_looking_up_unique_id`, `test_flag_on_refuses_user_without_row_only_when_unlinked`, `test_auth_event_never_stores_secrets_by_signature`
  - ORCID helper: `test_proposal_sync_uses_linked_orcid_for_mfid_user`, `test_resolve_user_by_orcid_finds_linked_user`, `test_post_users_refuses_orcid_linked_elsewhere`
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Migration** adds nullable `user.disabled_at` and `user.pending_at`.
- [ ] **Step 4: Implement,** replacing the user creation in `auth.py:117-145` and `hub_login.py:146-157` with the resolver. Catch the unique violation on concurrent first sign-in and retry the lookup once.
- [ ] **Step 5: Add** `POST /users/{user_ref}/disable` and `/enable` (admin; records an event; clears the auth cache for the user's keys; ends Hub sessions on disable).
- [ ] **Step 6: Run the full suite, commit, PR.** crucible-tests on staging.

## Task 6: Linking and unlinking

**Files:**
- Create: `app/auth/linking.py`, `app/api/routes/identities.py`
- Modify: `app/utils/hub_login.py`, `app/api/routes/hub_auth.py`, `app/api/main.py` (router)
- Test: `tests/test_auth_linking.py`

**Interfaces:**
- `POST /account/identities/{provider}/link`, with a Hub session and recent sign-in; body: `callback_uri`, `state`, `code_challenge`. Returns `{authorization_url}`, a one-time address on the API. API keys get 403.
- `GET /auth/hub/link/start`, opened by the browser: sets the login cookie, then redirects to the provider.
- The provider callback checks the cookie, validates the token and issues a `HubLoginCode` with `purpose="link"` plus the identity, then returns to the Hub. It writes no identity.
- `POST /auth/hub/link/confirm`, with the Hub's client secret in the `Authorization` header, and the code, the verifier and the user's Hub session token in the body. Writes the row only if the session's user is the transaction's user.
- The Hub's "Link" action is a POST with CSRF protection, and it shows a page naming the account before confirming. Send the Hub session this contract.
- `GET /account/identities`, `DELETE /account/identities/{id}`; admin `POST /users/{user_ref}/identities` (body: `issuer`, `subject`), `DELETE /users/{user_ref}/identities/{id}`.
- `unlink_identity(session, user: User, identity_id: int, *, actor: User) -> None`, raising `LastSignInMethod`.

- [ ] **Step 1: Write failing tests:**
  - `test_victim_opening_attackers_link_address_writes_nothing` (the attacker's start address, the victim's browser and Hub session; no row for either)
  - `test_attackers_login_cannot_be_linked_to_victims_account` (a link started in the victim's session, with the attacker's code and state)
  - `test_link_callback_without_the_browser_cookie_refused`, `test_start_address_is_single_use`
  - `test_link_confirm_with_another_users_session_refused`, `test_link_code_is_single_use`, `test_sign_in_code_cannot_confirm_a_link`, `test_link_code_cannot_sign_in`
  - `test_api_key_refused_on_link_and_unlink`, `test_stale_hub_session_gets_step_up_challenge`
  - `test_identity_of_another_user_refused`, `test_orcid_that_is_another_users_unique_id_refused`, `test_no_merge_ever`
  - `test_last_method_cannot_be_unlinked`, `test_unlink_allowed_with_password_or_other_identity`, `test_unlink_ends_other_hub_sessions`, `test_unlink_voids_live_link`
  - `test_every_link_and_unlink_writes_event`, `test_admin_link_records_actor`, `test_admin_link_refuses_service_account`
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run the full suite, commit, PR.**
- [ ] **Step 5: Ask the Hub session** for the sign-in methods page, the confirmation page under `/hub/account/`, and `/hub/auth/link/callback`, sending it the OpenAPI diff. Add that path to `HUB_AUTH_CALLBACK_URI` in the bundle and through the deployment session. The user merges both sides.

## Task 6b: Protect human administrators from API keys

Refuse with an API key, and keep for a Hub session with a recent sign-in: changing a human's admin status (`PATCH /users/{user_ref}/platform_role`), and reading or regenerating another administrator's key (`POST /users/{user_ref}/apikey`, `/apikey/regenerate`). Add the same check to the admin identity routes from Task 6 and the password links from Task 13. Add `python -m app.cli admin set-role`. Service account roles and revoking keys stay as they are.

- Tests: `test_api_key_cannot_change_a_humans_admin_status`, `test_api_key_cannot_regenerate_or_read_another_admins_key`, `test_api_key_can_still_regenerate_an_ordinary_users_key`, `test_api_key_can_still_revoke_an_admins_keys`, `test_api_key_can_still_set_a_service_account_role`, `test_recent_hub_session_can_do_all_of_these`, `test_cli_set_role_keeps_the_last_admin_rule`
- Tell the Hub and nano-crucible sessions, and add a changelog line, since scripts using these routes with a key will get 403.

## Task 7: Registration setting

**Files:**
- Create: `app/auth/registration.py`
- Modify: `app/auth/identity.py`, `app/auth/providers.py`, `app/config.py`, `app/api/routes/users.py`, `app/utils/hub_auth_maintenance.py`
- Test: `tests/test_auth_registration.py`

**Interfaces:**
- `registration_mode(provider: Provider | None) -> Literal["closed", "approval", "open"]`, where `None` means password.
- `POST /users/{user_ref}/approve`, `POST /users/{user_ref}/reject`, `pending` filter on `GET /users` (admin). Rejecting deletes the account.

- [ ] **Step 1: Write failing tests:** `test_closed_refuses_new_person`, `test_approval_creates_pending_with_no_session_or_key`, `test_open_creates_active`, `test_provider_override_beats_deployment_setting`, `test_implicit_orcid_defaults_open`, `test_open_for_passwords_refused_at_startup`, `test_pending_message_only_after_valid_sign_in`, `test_approve_clears_pending_and_writes_event`, `test_reject_deletes_account_and_identity`, `test_reject_refuses_active_user`, `test_switching_to_closed_keeps_pending_reviewable`, `test_stale_pending_removed_after_30_days`, `test_new_account_default_permissions_unchanged`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** The pending page is served by the API, with no session issued.
- [ ] **Step 4: Run the full suite, commit, PR.** Tell the Hub session about the pending filter for its admin area.

## Task 8: Cleanup job in the bundle

**Files:**
- Modify: `app/utils/hub_auth_maintenance.py:11-40`, `deploy/compose/compose.yaml`, `deploy/compose/README.md`
- Test: `tests/test_hub_auth_cleanup_job.py`

- [ ] **Step 1: Write failing tests:** `test_cleanup_removes_expired_password_tokens`, `test_cleanup_removes_address_rows_after_30_days`, `test_cleanup_purges_auth_event_addresses_and_failures_after_90_days`, `test_cleanup_purges_other_events_after_a_year`, `test_cleanup_keeps_credential_locks`, `test_zero_or_negative_retention_fails_settings_check`. Tables and rows not yet present from later tasks are added to this list by the task that creates them.
- [ ] **Step 2: Run to verify they fail, then implement.** Retention values come from settings, defaulting as above.
- [ ] **Step 3: Add an `auth-cleanup` service** beside `storage-cleanup`, with the same hourly pattern and the same hardening. CI's Compose smoke test checks it stays up.
- [ ] **Step 4: Run the full suite, commit, PR.** Tell the deployment session the Foundry's CronJob now covers more tables, with no command change.

## Task 9: API key hashing

`POST /users` stops creating a key nobody receives, since it cannot be read back once keys are hashed.

Clients first. Do not start until Task 2 has the answers and any client release has shipped; the user sets the date.

**Files:**
- Create: `migrations/data/`, one-time `backfill_api_keys` script
- Modify: `app/models/core_models.py`, migrations, `app/utils/auth_utils.py:89-127,169-198`, `app/api/routes/auth.py`, `app/api/routes/account.py:149-205`, `app/api/routes/users.py:62,305,468,690-714`, `app/api/routes/service_accounts.py:31-56`, `app/services/activity.py:47`, `app/cli/admin.py`, `deploy/compose/README.md:259`
- Test: `tests/test_api_key_hashing.py`, and existing key tests

**Interfaces:**
- `create_api_key` and revocation write `auth_event` records.
- Model `ApiKey`: columns as in the spec, plus `access_group_id`, kept from `ApiToken`.
- `create_api_key(session, user: User, *, name: str | None = None, expires_at: datetime | None = ...) -> str`; default expiry 365 days, matching today's one-year rule in `find_key`.
- `find_api_key(session, presented: str) -> ApiKey | None`, by `sha256(presented)`.

Three pull requests, each deployable alone:

- [ ] **Step 1: Expand.** Failing tests: `test_new_key_is_written_to_both_tables`, `test_hash_is_sha256_of_key`, `test_plain_key_not_in_new_table`. Then the table, double writes, and a one-time script in `migrations/data/`, which sets `expires_at = token_created + 365 days`, so no key's life changes. With the user's approval, migrate and backfill on staging and then production.
- [ ] **Step 2: Switch reads.** Failing tests: `test_existing_key_still_authenticates_after_backfill`, `test_expired_key_refused_identically`, `test_revoked_key_refused_within_cache_ttl`, `test_service_account_key_works`, `test_several_keys_per_user`, `test_named_key_listed_with_prefix_only`, `test_last_used_updates_new_table`. Then lookups, `/account/verify`, key status, activity tracking and the cache use `ApiKey`. `test_invalid_key_is_401_never_a_redirect`. Add `POST /account/apikeys` (body `name`, optional `expires_at`; returns `id`, `key_prefix`, `api_key` once, `no-store`), `GET /account/apikeys` (without the key), `DELETE /account/apikeys/{id}`. `/account/verify` describes the presented key, and with a Hub session the newest active key, as documented today. `api_key_status` keeps its shape and describes the newest active key. Send these shapes to the nano-crucible session before merging.
- [ ] **Step 3: Stop showing keys again.** Only after the agreed client release. `/auth/apikey` and `/user_apikey` stay, and offer "create a new key" by POST, which adds a key and never replaces one; `GET /account/apikey` answers 404 with instructions in `detail`. Tests: `test_apikey_page_never_shows_an_existing_key`, `test_post_adds_a_key_and_shows_it_once`, `test_post_leaves_existing_keys_working`, `test_account_apikey_is_404_with_instructions`, `test_email_prompt_page_still_works`.
- [ ] **Step 4: Contract.** A later release, with the user's approval: drop `apitoken`. Update the backup note at `README.md:259`, since backups no longer contain usable keys.
- [ ] **Step 5: crucible-tests on staging at each step, then a production check** through the deployment session.

## Task 10: Password hashing and rules

No routes yet.

**Files:**
- Create: `app/auth/passwords.py`, `app/auth/data/blocked_passwords.txt`, `scripts/build_password_blocklist.py`, a migration
- Modify: `pyproject.toml` (`pwdlib[argon2]==0.3.1`), `app/models/core_models.py`, `app/config.py`, `docs/self_hosting_secrets.md`
- Test: `tests/test_auth_passwords.py`

**Interfaces:**
- Model `LocalCredential`, columns as in the spec.
- `hash_password(password: str) -> tuple[str, str | None]`, returning the hash and pepper ID.
- `verify_password(password: str, stored_hash: str, pepper_id: str | None) -> tuple[bool, tuple[str, str | None] | None]`, returning success and any upgraded hash.
- `verify_dummy(password: str) -> None`, the same work against a fixed hash.
- `check_new_password(password: str, *, context: Iterable[str]) -> PasswordProblem | None`, where `PasswordProblem` has a `code` and a user-facing `message`.
- Settings: `CRUCIBLE_AUTH_ARGON2_MEMORY_KIB`, `_TIME_COST`, `_PARALLELISM`, secret `CRUCIBLE_AUTH_PEPPER` with `CRUCIBLE_AUTH_PEPPER_ID`, and `CRUCIBLE_AUTH_PREVIOUS_PEPPERS` for rotation.
- Normalizing and peppering, in this order, are not left to choice:

```python
normalized = unicodedata.normalize("NFC", password).encode("utf-8")
prepared = base64.b64encode(hmac.new(pepper, normalized, hashlib.sha256).digest()).decode("ascii")
```

- [ ] **Step 1: Write failing tests:**
  - `test_parameters_below_floor_fail_settings_check` (m=19456 KiB, t=2, p=1)
  - `test_hash_is_argon2id_phc_string`, `test_two_hashes_differ`
  - `test_composed_and_decomposed_forms_verify` (`é` as U+00E9 and U+0065 U+0301)
  - `test_spaces_preserved`: `"  pass phrase here long  "` does not verify with the spaces removed
  - `test_15_code_points_accepted_14_refused`, `test_emoji_count_as_one_each`, `test_256_accepted_257_refused`
  - `test_no_composition_rule`, `test_blocklist_whole_match_case_insensitive`, `test_blocklist_reason_given`, `test_context_username_email_names_service_refused`
  - `test_rehash_returned_when_parameters_raised`, `test_rehash_returned_when_pepper_rotated`, `test_old_pepper_verifies_during_rotation`, `test_wrong_pepper_fails`, `test_missing_pepper_refuses_startup_when_passwords_enabled`, `test_empty_pepper_refuses_startup_when_passwords_enabled`
  - `test_empty_password_never_verifies`, `test_missing_or_empty_stored_hash_never_verifies`
  - `test_verify_dummy_takes_comparable_time`
  - `test_hash_not_in_user_read_or_any_response_schema`
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Measure parameters** on a machine like the bundle's target. Record them in the PR. Target: well under a second, above the floor.
- [ ] **Step 4: Build the blocklist** with the script, from a public list whose license allows redistribution, recording source and license in the file's header. Filter to 15 or more characters, plus a short list of predictable long phrases.
- [ ] **Step 5: Implement, run the full suite, commit, PR.**

## Task 11: Attempt limits

**Files:**
- Create: `app/auth/throttle.py`, a migration
- Modify: `app/models/core_models.py`, `app/auth/passwords.py`, `app/config.py`
- Test: `tests/test_auth_throttle.py`, `tests/test_auth_throttle_postgres.py`

**Interfaces:**
- Model `LoginThrottle`, and the failure columns on `LocalCredential`, as in the spec.
- `begin_attempt(engine, credential_user_id: int | None, client_address: str | None, *, device_cookie: DeviceCookie | None, now: datetime) -> AttemptDecision`: one short committed update, returning `check_password: bool`. No lock or connection is held afterwards.
- `finish_attempt(engine, decision: AttemptDecision, *, success: bool, now: datetime) -> None`.
- `issue_device_cookie(user_id: int) -> str`, `read_device_cookie(value: str | None, user_id: int) -> DeviceCookie | None`: following OWASP's device cookie design, signed with `itsdangerous.URLSafeTimedSerializer` under its own salt (`crucible.device-cookie`), not a hand-written HMAC. Lifetime 90 days by default.
- `hash_slot()`: a per-worker semaphore around hashing.
- `client_address(request) -> str | None`: reads the rightmost address appended by the trusted proxy, using `CRUCIBLE_AUTH_TRUSTED_PROXY_HOPS` (default 0, the bundle sets 1).

- [ ] **Step 1: Write failing tests:**
  - Schedule: `test_first_four_failures_no_delay`, `test_delay_doubles_from_2_seconds`, `test_delay_caps_at_900_seconds`, `test_success_resets`
  - `test_attempt_during_delay_not_counted_and_password_not_checked`, `test_correct_password_during_delay_fails`
  - `test_lock_at_100_counted_failures`, `test_100_failures_take_about_a_day_at_the_cap` (simulated clock), `test_locked_refuses_correct_password`
  - Device cookie: `test_cookie_bypasses_account_delay`, `test_cookie_for_other_user_ignored`, `test_tampered_cookie_ignored`, `test_cookie_has_own_failure_limit`, `test_locked_credential_refuses_even_with_cookie`, `test_session_or_user_token_signature_is_not_a_device_cookie`, `test_zero_or_negative_cookie_lifetime_fails_settings_check`
  - Address: `test_address_limit_applies_across_accounts`, `test_unknown_usernames_count_against_address`, `test_spoofed_forwarded_header_does_not_change_address`, `test_address_row_removed_after_30_days`
  - Postgres: `test_parallel_guesses_cannot_exceed_counter` (20 threads, exact count), `test_no_connection_held_during_hash`
  - `test_rename_keeps_lock_and_does_not_pass_it_on`, `test_freed_username_gets_no_counters`
- [ ] **Step 2: Run to verify they fail, implement, run the full suite, commit, PR.**

## Task 12: Sign-in form

**Files:**
- Modify: `app/api/routes/hub_auth.py`, `app/utils/hub_login.py`, `app/api/routes/account.py:104-120`, `app/api/routes/users.py:617-637`
- Test: `tests/test_auth_password_sign_in.py`

**Interfaces:**
- `GET` and `POST /auth/hub/password`. The POST takes form fields `username`, `password`, `state`. It validates the login record without consuming it through `peek_login(engine, state, browser_secret) -> HubLoginTransaction`, and consumes it only on success.
- The failure text is fixed: "Sign-in failed. Check your details, or try again in a few minutes."
- Headers on every response: `Cache-Control: no-store`, `Referrer-Policy: no-referrer`, `Content-Security-Policy: default-src 'none'; style-src 'self'; form-action 'self'; frame-ancestors 'none'`.

- [ ] **Step 1: Write failing tests:**
  - `test_all_failures_give_identical_body_and_status`, parametrized: unknown user, wrong password, delayed, locked, disabled, no password set, service account
  - `test_timing_comparable_across_failures` (median of 20, generous bound, marked for CI stability)
  - `test_marker_password_in_no_log_or_response`, through every failure path, including malformed requests, capturing logs
  - `test_typo_keeps_login_record`, `test_success_consumes_login_record`, `test_form_past_login_lifetime_says_expired_and_is_not_counted`
  - `test_post_without_browser_cookie_refused` (CSRF), `test_get_never_authenticates`
  - `test_passwords_with_plain_http_public_address_refuse_startup`, `test_localhost_http_allowed`, `test_trusted_proxy_hops_below_zero_refuses_startup`
  - `test_uppercase_and_spaced_username_finds_account`, `test_password_never_altered`
  - `test_password_disabled_route_is_404`, `test_rehash_applied_on_success`, `test_auth_time_is_set_so_step_up_works`, `test_step_up_with_max_age_shows_form_again`
  - `test_device_cookie_set_on_success`, `test_form_has_autocomplete_and_allows_paste`, `test_show_password_works_under_csp`
  - Username: `test_clearing_username_refused_with_password`, `test_rename_needs_recent_sign_in_with_password`
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement.** Styles and the small show-password script are served as static files under `/auth/hub/`, so the policy needs no inline allowance; the script hash or `script-src 'self'` is added accordingly.
- [ ] **Step 4: Run the full suite, commit, PR.**

## Task 13: Links, changes, admin routes, password registration and CLI

**Files:**
- Create: `app/auth/password_links.py`, `app/api/routes/passwords.py`, `app/cli/auth.py`, a migration
- Modify: `app/cli/admin.py`, `app/cli/auth.py`, `app/utils/hub_auth_maintenance.py`
- Test: `tests/test_auth_password_links.py`, `tests/test_auth_credential_routes.py`, `tests/test_auth_password_registration.py`

**Interfaces:**
- Model `PasswordToken`, columns as in the spec, with partial unique index on `user_id` where `consumed_at IS NULL AND voided_at IS NULL`.
- `issue_link(session, user: User, purpose: Literal["setup", "reset"], *, actor: User | None, lifetime: timedelta = timedelta(hours=24)) -> str`, voiding any live link in the same transaction.
- `void_links(session, user: User) -> None`.
- Routes per the spec's API routes section. `GET` and `POST /auth/hub/password-link`; `GET` and `POST /auth/hub/register`; `POST /account/password`; `POST /users/{user_ref}/password-link`; `DELETE /users/{user_ref}/password`.
- `require_credential_change(principal, *, target: User)` in `auth_utils.py`, implementing the spec's table of who may do what.
- Refusals carry a stable `detail.code`: `api_key_not_allowed` and `admin_target_not_allowed` (403), `current_password_invalid` (422, never 401). New admin routes accept the `Crucible-Privilege-Mode` header through `authorize_user`, like the others.
- CLI: `admin password-link --user REF`, `admin create` prints a link when passwords are on, `auth revoke-passwords [--commit]`, `auth rewrite-issuer --from URL --to URL [--commit]`.

- [ ] **Step 1: Write failing tests:**
  - Links: `test_opening_link_does_not_consume`, `test_first_open_moves_token_to_cookie_and_redirects`, `test_single_use`, `test_expired_refused`, `test_new_link_voids_old`, `test_one_live_link_under_concurrency` (Postgres), `test_only_hash_stored`, `test_link_token_absent_from_logs`
  - Effects: `test_use_sets_password_and_clears_lock`, `test_use_ends_all_hub_sessions`, `test_use_refuses_handoff_codes_issued_before`, `test_no_automatic_sign_in`, `test_voided_by_disable_password_change_and_identity_change`
  - Setup link: `test_setup_link_works_when_registration_closed`, `test_setup_link_form_needs_csrf_token`, `test_two_tabs_cannot_both_use_a_link`, `test_disabled_login_never_reaches_account_creation`
  - Service accounts: `test_service_account_refused_everywhere`
  - Credential table, one test per row, with `@pytest.mark.parametrize` over API key and Hub session: admin key can issue a link and it is logged; admin key refused on another admin; own key plus correct current password can change; own key alone refused; wrong current password counts on the account counter; key refused for first password, link and unlink; Hub session needs a recent sign-in
  - Change: `test_change_ends_other_sessions`, `test_change_applies_password_rules`, `test_first_password_needs_recent_sign_in_and_generates_username`, `test_password_events_never_touch_api_keys`
  - Lifetimes: `test_zero_or_negative_link_lifetime_fails_settings_check`
  - Address limit: `test_address_limit_applies_to_opening_and_submitting_links`
  - Registration: `test_password_register_creates_pending`, `test_register_refused_unless_approval`, `test_register_applies_rules_and_address_limit`, `test_pending_sees_waiting_only_with_correct_password`
  - Contract: `test_wrong_current_password_is_422_with_code`, `test_refusals_have_stable_codes`, `test_elevated_header_accepted_on_new_admin_routes`, `test_link_response_is_no_store`
  - CLI: `test_admin_create_prints_link`, `test_admin_password_link_recovers_locked_sole_admin`, `test_revoke_passwords_dry_run_by_default`, `test_rewrite_issuer_dry_run_by_default`
- [ ] **Step 2: Run to verify they fail, implement, run the full suite, commit, PR.**
- [ ] **Step 3: Ask the nano-crucible session** for `user setup-link`, `user reset-link`, `user approve`, `user disable`, and `account password`, and the Hub session for its password and admin screens, sending the OpenAPI diff.

## Task 14: Independent security review

A gate, not code.

- [ ] **Step 1: Run an independent review** of Tasks 1 to 13 by a fresh reviewer given the code and the spec, without my conclusions, plus `/security-review`. It checks each requirement in the spec separately, and tries to disprove each mismatch before reporting it, with evidence to the line (method borrowed from Trail of Bits' `spec-to-code-compliance`, which is not installed). The user decides whether to add `/code-review ultra` or an outside professional, which is worth considering if labs outside LBL will run it.
- [ ] **Step 2: Fix or record every finding,** with the user deciding anything left open.
- [ ] **Step 3: Check the spec's "Tests that must exist" list** against the suite, line by line.

## Task 15: Compose default and release

**Files:**
- Modify: `deploy/compose/compose.yaml`, `deploy/compose/.env.example`, `deploy/compose/README.md`, `deploy/compose/Caddyfile`, `docs/self_hosting_design.md` (Sign-in section points to the plan), `CHANGELOG.md`, version file

- [ ] **Step 1: Write the failing smoke test** in the CI Compose job: fresh install, `admin create --no-input`, use the printed link, sign in with the password through the form, and get a Hub session through the Hub bundle, with `auth-cleanup` running.
- [ ] **Step 2: Set** `CRUCIBLE_AUTH_PASSWORD_ENABLED=true`, `CRUCIBLE_AUTH_TRUSTED_PROXY_HOPS=1`, and the pepper secret in the bundle. Setup generates the pepper. The guide says to back it up apart from database backups.
- [ ] **Step 3: Rewrite the install guide** around HTTPS: real domain as the default, DNS challenge for internal names, single-machine option with its certificate warning, and what works without any of them. Add sections: adding ORCID or an institution, plugging in Authentik or Keycloak, changing registration, recovering a locked administrator.
- [ ] **Step 4: Confirm the Foundry's deployment does not set** `CRUCIBLE_AUTH_PASSWORD_ENABLED`, with the deployment session.
- [ ] **Step 5: Run the full suite and the smoke test, commit, PR, and open the release PR.** The user merges, and tagging happens on the user's instruction.
- [ ] **Step 6: Update the logbook,** and remove the Sign-in text the plan replaced in the design document.

## Self-review notes

- Spec coverage checked section by section. Registration is Tasks 5, 7, 13; credential table is Task 13; cleanup is Task 8, extended by later tasks; recent-sign-in changes are Task 4.
- Task 3 uses `hub.orcid_issuer`, and Task 1 deliberately does not, so Task 1 cannot change behaviour on any environment.
- A fact the spec omitted, kept here: API keys expire after a year today (`find_key`), and Task 9 preserves that through `expires_at`.
