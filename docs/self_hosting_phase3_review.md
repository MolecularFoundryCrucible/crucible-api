# Task 14: review of Phase 3 against the spec

Three independent reviewers (Fable) checked Tasks 1 to 13 against `self_hosting_phase3_plan.md`, `self_hosting_phase3_implementation.md` and `self_hosting_sign_in_clients_design.md` on 2026-11-09, requirement by requirement, at main `346a0b2` plus the open server-commands PR. 226 requirements: 180 met, 21 partly met, 10 not met, 15 superseded by later decisions. Their reports follow unchanged; the reproduction scripts they cite were local and are not kept.

## Status, decided by the user on 2026-11-09

| Finding | Decision | Where |
| --- | --- | --- |
| Refresh grace renewable by a copied token | Fixed: retired siblings get no grace | #478 |
| Unlinking ignores a password | Fixed | #478 |
| Approve and reject accept a stale session | Fixed | #478 |
| Older ORCID flow defaults registration to open | Fixed: closed | #478 |
| Caddy 8 KB limit missing on the API's sign-in forms | Fixed | #478 |
| Password sign-in failures and locks not in the sign-in history; no 90-day address purge | Record failures and locks without client addresses; the 90-day address rule is dropped, since no address is stored | next PR |
| No admin reader of the sign-in history | `GET /users/{user_ref}/auth-events` | next PR |
| Device grant not opt-in per client; device-code entry limited per address only; no CSRF check on the code form; no `api_key_id` or token-issue events; older keys without `crucible_` prefix; `created_by` on `POST /users` | Open, for the user to schedule | |
| Metadata served under the API path, 410 stubs, device sessions refused, required email, Task 5a stages B4b to E | Superseded by recorded decisions; the spec text is updated | |



# Task 14 review, part 1: identities, providers, linking, registration and the ID move

Reviewer: independent, given the code and the spec only. Code: `scratchpad/review-main` (a checkout of `main`). Spec: `spec/self_hosting_phase3_plan.md` (the plan), `spec/self_hosting_phase3_implementation.md` (the tasks), `spec/self_hosting_sign_in_clients_design.md` (Task 9b). Where a dated decision in those files changed a requirement, the decision is the requirement.

Method: every requirement in the sections assigned to me was listed, the code implementing it was read (not summaries), and each suspected mismatch was first attacked (middleware, dependencies, constraints, other modules, tests) before it was kept. Two mismatches were reproduced with local tests on the SQLite test engine (`scratchpad/task14/probe_identities/test_probe.py`, run with `PYTHONPATH=. uv run pytest -p tests.conftest ...` from the checkout; 2 passed, meaning both suspected behaviours were observed). Nothing in the checkout was modified, committed or checked out; `uv run` created the checkout's ignored `.venv`.

Verdict counts (table below): 84 requirements. Met 70, partly met 6, not met 5, superseded 3. Six confirmed mismatches follow in section 2 (two of them, M1 and M2, reproduced locally; M6 was already recorded as an open item).

## 1. Requirement table

Line references are to the plan (`plan:N`) or the implementation file (`impl:N`). Code references are `file:line` in the checkout.

### A. Data model: `user_identity`, double writes, backfill (Task 3)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 1 | New tables reference `user.id`, never `user.orcid` (plan:40) | Met | `core_models.py:312` (`user_identity.user_id` FK `user.id`), `:379` (`auth_event.user_id`), `:327` (`local_credential`). |
| 2 | `user_identity` columns: bigint id; user_id not null, cascade, indexed; issuer text; subject varchar(255); label varchar(255); created_at; created_by_user_id set null; verified_at; last_used_at (plan:46-56) | Met | `core_models.py:304-319`; migration `20261103_7c1e4a9d2f60:14-27`, `verified_at` added in `20261105_c5f18d2e6a37` (sign-in foundation; `tests/test_auth_foundation.py::test_sign_in_foundation_migration_roundtrip`). |
| 3 | Unique (`issuer`, `subject`); partial unique index on `user_id` for the two ORCID issuers; other issuers may repeat (plan:58, impl:117) | Met | `core_models.py:307-308` (`uq_user_identity_issuer_subject`, `ux_user_identity_one_orcid`), migration `:27-28`; tests `test_issuer_and_subject_are_unique_in_the_database`, `test_one_person_cannot_hold_two_orcids_in_the_database`, `test_several_identities_at_a_non_orcid_issuer_are_allowed`, `test_constraints_hold_on_postgres`. |
| 4 | Email and names are not stored in `user_identity` and never used to match; provider tokens not stored; config names not stored (plan:60) | Met | Model has no email/name columns; `sign_in.py:32-46` matches on issuer+subject only; `providers.py:161-173` discards tokens, keeps claims only in memory. Tests `test_no_match_by_email_when_emails_equal`, `test_no_match_by_username`. |
| 5 | `ensure_identity(...)` idempotent, raises `IdentityTaken` when held by another user (impl:105) | Met | `identity.py:53-78`; tests `test_ensure_identity_is_idempotent`, `test_ensure_identity_raises_when_held_by_another_user`, `test_a_failed_identity_write_leaves_the_outer_transaction_usable`. |
| 6 | Every path that creates or signs in an ORCID user writes the row: Hub, `/auth/apikey`, `POST /users` with ORCID, `admin create --orcid` (plan:158, impl:96) | Met | All creation goes through `provision_user` (`user_utils.py:167-189`, writes the row at `:187`); sign-in through `resolve_sign_in` (`sign_in.py:72`). Tests `test_hub_sign_in_writes_the_identity_row_for_new_and_existing_users`, `test_apikey_page_writes_the_identity_row_with_the_older_flows_issuer`, `test_post_users_with_orcid_writes_the_identity_row`; `admin create` is covered only indirectly (`test_admin_create_refuses_an_orcid_linked_to_another_user`), see missing tests. |
| 7 | Service accounts never get an identity row (impl:113) | Met | `provision_user` is for people only (`is_service_account=False`, `:173`); service accounts are created elsewhere; `test_service_account_gets_no_identity_row`. |
| 8 | Backfill script `20261103_backfill_user_identity.py --issuer URL [--commit]`: dry run default, insert only, batches of 1000, non-service ORCID humans, idempotent, prints counts (plan:159, impl:119) | Met | `migrations/data/20261103_backfill_user_identity.py:27-79`; tests `test_backfill_dry_run_writes_nothing`, `test_backfill_inserts_only_orcid_humans`, `test_backfill_can_run_twice`, `test_backfill_requires_the_issuer`, `test_backfill_covers_every_batch`, `test_backfill_never_changes_a_user`. |
| 9 | Backfilled rows have empty `verified_at` until the next sign-in (plan:55) | Met | Backfill inserts only `user_id, issuer, subject, created_at` (`:75`); resolver sets `verified_at` on sign-in (`sign_in.py:80`). |
| 10 | `POST /users` and linking check both `user_identity` and `unique_id` (plan:162) | Met | `users.py:524-528` (unique_id, identity group, `find_identity`); `linking.py:166-176` (`link_status` checks row, second ORCID, and `unique_id` holder). |
| 11 | `POST /users` records the administrator in `created_by_user_id` (plan:206, impl:291) | Not met | `users.py:573-577` calls `provision_user` with no actor; `provision_user` calls `ensure_identity` without `created_by` (`user_utils.py:187`). Already recorded as open item 1.11 (impl:253). |

### B. Resolver, lookups, disabled and pending (Task 5)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 12 | One resolver, no exceptions: `/auth/apikey` and the Hub handoff both use it (plan:30) | Met | `hub_login.py:167` (`create_handoff_code`), `auth.py:95` (`_record_signed_in_user`), `browser_login` uses `auth.py`; `oauth` routes share `hub_auth._finish_sign_in` (`hub_auth.py:269-305`). Tests `test_hub_uses_the_resolver_for_an_mfid_person`, `test_apikey_page_gives_an_mfid_person_their_own_key`. |
| 13 | Step 1: look up (issuer, subject); if found and disabled, refuse and stop (plan:149) | Met | `sign_in.py:55-57`; `test_disabled_refused_at_the_resolver_and_never_continues`. |
| 14 | Step 2: unlinked ORCID held as `unique_id` is refused with a message saying an administrator can help (plan:150) | Met | `sign_in.py:40-45` (`UNLINKED_ORCID`); `/auth/apikey` returns 409 "contact an administrator" (`auth.py:25,106`); Hub gets standard `access_denied` (decision 10, plan:375). Tests `test_unlinked_orcid_is_refused`, `test_apikey_page_refuses_an_unlinked_orcid`, `test_unlinked_orcid_at_the_hub_is_access_denied`. |
| 15 | Step 3: with `open` or `approval`, create account and identity in one transaction, pending when `approval` (plan:151) | Met | `sign_in.py:63-72` inside the caller's transaction; `test_approval_creates_a_waiting_account_with_its_login`, `test_open_creates_a_working_account`, `test_refused_sign_in_changes_nothing`. |
| 16 | Step 4: otherwise refuse and record; never match by email, username or name (plan:152) | Met | `sign_in.py:63-64` (`NOT_LINKED`), `record_refused_sign_in` (`:92-95`); `test_closed_refuses_someone_new_and_creates_nothing`, `test_hub_refusal_keeps_the_event_and_issues_no_code`. |
| 17 | With `required_user`, only look up, never create; wrong login leaves no stray account (plan:154, impl:157) | Met | `sign_in.py:58-59` before the registration step; `test_required_user_never_creates_anyone`, `test_step_up_with_unknown_login_creates_nothing`, `test_required_user_mismatch_issues_no_code_and_returns_login_required`. |
| 18 | ORCID-shaped `sub` from another provider gets no ORCID treatment (plan:133) | Met | `sign_in.py:62,69` keys everything on `issuer in ORCID_ISSUERS`; `test_orcid_shaped_sub_from_other_provider_gets_no_orcid_treatment` (resolver and Hub flow). |
| 19 | Lookups switch behind `CRUCIBLE_AUTH_IDENTITY_LOOKUP` (off by default); off keeps ORCID lookups on `unique_id`; linking refused with 409 while off (impl:274) | Met | `config.py:311`; `sign_in.py:36-37`; `linking.py:71-73`, `identities.py:43-46,70-73`. Tests `test_flag_off_keeps_looking_up_unique_id`, `test_flag_on_...`, `test_linking_waits_for_the_identity_lookup_setting`. |
| 20 | Concurrent first sign-in: catch the unique violation and retry once (impl:306) | Met | `identity.py:18-24`, used by `hub_login.py:183`, `auth.py:103`; Postgres tests `test_simultaneous_first_hub_sign_ins_create_one_person_on_postgres`, `test_simultaneous_first_apikey_sign_ins_create_one_person_on_postgres`. |
| 21 | `orcid_of` reads only `user_identity`, no fallback to `unique_id` (plan:203, impl:289) | Partly met | `identity.py:39-41` is exact. But `linked_orcid` (`identity.py:44-50`) and `User.orcid` (`core_models.py:284-292`) fall back to an ORCID-shaped `unique_id` while the lookup flag is off. Deliberate rollout bridge, tested (`test_sync_before_the_backfill_still_uses_an_orcid_shaped_id`, `test_orcid_shaped_unique_id_counts_only_until_the_lookup_setting_is_on`); goes away when the temporary flag is removed. |
| 22 | `user.disabled_at`; checked on sign-in by any method, handoff redemption, Hub refresh, Hub access token, API keys within the cache lifetime (plan:120) | Met | Resolver `sign_in.py:56`; password `hub_auth.py:487`; redemption `hub_tokens.py:104-106` via `_may_hold_hub_tokens` (`:49-50`); refresh `:180-182`; access `:233-235`; API key `auth_utils.py:237`. Tests in `tests/test_auth_disable.py` (all five). |
| 23 | `user.pending_at`; a pending user gets no session or API key on any path (plan:118) | Met | `_may_hold_hub_tokens` (`hub_tokens.py:50`), API key check `auth_utils.py:237`, handoff writes no code (`hub_login.py:168-170`), `/auth/apikey` shows the pending page before any key (`auth.py:97-98,123`), password path `hub_auth.py:538-541`. `test_waiting_accounts_get_no_hub_tokens_or_api_key`. |
| 24 | `assert_active`-style check raising the generic failure for disabled or pending (impl:293) | Met (different shape) | No function named `assert_active`; the checks are inline at each path (row 22, 23). Behaviour matches. |
| 25 | A provider-created user with an MFID gets a generated username; every person created has a username (plan:119, impl:294) | Met | `user_utils.py:171` (`generate_unique_username`); `test_provision_generates_a_username_when_none_is_given`, `test_new_person_from_hub_sign_in_gets_a_username_and_identity_group`. |
| 26 | `POST /users/{ref}/disable` and `/enable`: admin, record an event, clear cached keys, end Hub sessions on disable (impl:307); Hub sessions need a recent sign-in (impl:357) | Met | `users.py:725-761`: `revoke_all_hub_tokens`, `void_links`, `record_event`, `forget_cached_keys`; `_account_to_switch` calls `require_recent_authentication` (`:681`). Tests `test_admin_disables_and_everything_stops_at_once`, `test_enable_restores_sign_in_but_not_old_sessions`, `test_stale_hub_session_gets_a_step_up_for_a_human_administrator`. |
| 27 | Last active human administrator cannot be disabled | Met | `users.py:732-738`; `test_cannot_disable_the_last_active_human_administrator`. |
| 28 | `auth_event`: columns as specified; insert only; never holds a password, token, hash or typed username; failure on an unknown name recorded without a user; also logged (plan:103-114) | Met | `core_models.py:369-384`; `record_event` takes no free text (`events.py:34-46`), logs at `:46`; `test_auth_event_never_stores_secrets_by_signature`, `test_record_event_without_a_user_is_allowed`, `test_record_event_snapshot_outlives_the_user`. `client_id` column added (sign-in clients design, section 4). |
| 29 | Event retention is a setting: addresses and failures 90 days, the rest one year (plan:114) | Partly met | One setting, `CRUCIBLE_AUTH_EVENT_RETENTION_DAYS` (default 365, `config.py:312`), applied to every row (`hub_auth_maintenance.py:49-56`). No 90-day purge of `client_address` or failure events. See mismatch M4. |
| 30 | `GET /users/{user}/auth-events` admin route; the Hub shows link and unlink events under sign-in methods (plan:201, plan:291) | Not met | No route reads `auth_event` (`grep AuthEvent app/api/routes` finds only writers). See mismatch M5. |
| 31 | Proposal sync uses the linked ORCID whether verified or not; 502 on outage; `POST /users` carries on; shared membership function (plan:206-207) | Met | `tests/test_proposal_sync.py::test_sync_works_for_an_unverified_orcid`, `::test_sync_finds_nothing_after_unlink`, `::test_sync_returns_502_when_the_proposal_service_is_down`, `::test_creating_a_person_carries_on_when_the_service_is_down`. |
| 32 | Foundry: an existing ORCID user signs in, keeps `unique_id`, `/auth/apikey` still works before and after the backfill (plan:351) | Met | `test_foundry_user_signs_in_and_keeps_unique_id_after_backfill`, `test_flag_off_keeps_looking_up_unique_id`, `test_apikey_page_writes_the_identity_row_with_the_older_flows_issuer`. |

### C. Every person's `unique_id` becomes an MFID (Task 5a)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 33 | `unique_id` becomes an MFID for everyone; ORCID is a linked login; every new person gets an MFID from the one creation function (plan:409, impl:198) | Not met (scheduled, not yet built) | `sign_in.py:69` still sets `unique_id=subject` for an ORCID sign-in; `users.py:522` and `admin.py:134` still accept an ORCID as `unique_id`. impl:231 records that only PR A, B1 to B4a of 5a are built; B4b, C, D, E remain. Consistent with the recorded order (impl:217: rewrite comes "after the rest of Phase 3"). Reported so the gate knows the state. |
| 34 | New code never treats `unique_id` as an ORCID and never uses it to decide who someone is (plan:42) | Partly met | Sign-in decides through `user_identity` once the flag is on (row 19). Remaining `is_orcid(unique_id)` decisions: the rollout bridge in row 21, and `resolve_sign_in` refusing an ORCID-shaped `unique_id` holder (`sign_in.py:40`, which is the spec's own step 2). Both are transitional and recorded. |
| 35 | Internal IDs everywhere; `Principal.internal_user_id`; ownership checks by `user.id` (impl:205, impl:231) | Met for the built stages | `auth_utils.py:727-735` (`caller_row`, `is_caller`); `users.py` approve/disable use `caller_row`. B4b onward is outside this review's area and unbuilt. |
| 36 | The API accepts an ORCID wherever it accepts a person, through `user_identity` (impl:199, impl:229) | Met | `user_utils.py:250-268` (`user_by_id_or_orcid`, `person_by_reference`); `test_resolve_user_by_orcid_finds_linked_user` equivalent `test_linked_orcid_finds_a_person_whose_id_is_an_mfid`, `tests/test_orcid_input.py`. |

### D. Generic OIDC providers (Task 4)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 37 | Providers are a typed list `CRUCIBLE_AUTH_PROVIDERS`; every field required; unknown fields refused; no implicit provider; a complete example shipped (plan:135, impl:128) | Met | `config.py:214-256` (`extra="forbid"`), `:326-346`; `deploy/compose/auth-providers.example.json`; tests `test_every_field_is_required`, `test_unknown_field_is_refused`, `test_no_provider_is_implied_by_the_older_orcid_settings`, `test_published_example_is_accepted`. |
| 38 | ORCID status comes only from the exact issuer (read-only `is_orcid`); `subject_is_orcid` dropped; only one ORCID provider; `legacy_callback` only for ORCID (plan:133, impl:128) | Met | `config.py:245-253`, `:342-345`; tests `test_orcid_status_comes_only_from_the_exact_issuer`, `test_old_subject_is_orcid_field_is_refused`, `test_legacy_callback_needs_orcid`, `test_duplicates_are_refused`. Supersedes "`subject_is_orcid` refused on another issuer" (plan:343). |
| 39 | Hub on and no list: settings check fails, Hub sign-in answers 503 (plan:135) | Met | `config.py:418-420` (`HubSettings.problems`, also satisfied by passwords on); `test_hub_without_a_provider_list_is_a_missing_setting`. |
| 40 | Authlib FastAPI integration with `server_metadata_url`; no hand-written URL building or token parsing (plan:139) | Met | `providers.py:85-94` (`OAuth().register(... server_metadata_url=...)`), `:141` (`create_authorization_url`), `:163-166` (`fetch_access_token`, `parse_id_token`). |
| 41 | Discovery read on first use; issuer must match exactly; HTTPS except localhost (plan:140) | Met | `providers.py:68-70,113-122`; `config.py:229-236`; tests `test_issuer_mismatch_marks_only_that_provider_unavailable`, `test_discovery_address_drops_a_trailing_slash_but_the_issuer_stays_exact`, `test_http_is_allowed_for_localhost`, `test_bad_values_are_refused`. |
| 42 | Only asymmetric algorithms the provider advertises; never `none` or HS256 (plan:141) | Met | `providers.py:22,75-82` filters the advertised list; tests `test_hs256_signed_with_the_client_secret_is_refused`, `test_algorithm_not_advertised_refused`, `test_unsafe_token_algorithms_are_refused_even_when_advertised`, `test_only_unsafe_algorithms_refuses_sign_in`. |
| 43 | Check signature, `iss`, `aud`, `exp`, `nonce` on every token; a token missing `sub` is refused (plan:142) | Met | `providers.py:53-57,164-171` (claims options, `audience_is_only`, `sub` check; Authlib checks `exp` and `nonce`); tests `test_wrong_tokens_are_refused`, `test_token_signed_with_another_key_is_refused`, `test_response_without_an_id_token_is_refused`. |
| 44 | Always send `state` and `nonce`; PKCE S256 when advertised (ORCID does not) (plan:143) | Met | `providers.py:129-142`; tests `test_pkce_sent_when_advertised`, `test_pkce_omitted_for_orcid`, `test_chooser_gives_each_method_its_own_state`. |
| 45 | Mix-up defence: one callback per provider, the login record stores the provider, a callback for another provider is refused; RFC 9207 `iss` check (plan:144, impl:314) | Met | `hub_auth.py:258-264` (`_callback_provider`), `providers.py:149-158` (`check_response_issuer`); tests `test_callback_for_other_provider_refused`, `test_legacy_callback_only_serves_the_provider_marked_for_it`, `test_response_from_another_issuer_is_refused`, `test_provider_that_advertises_iss_must_send_it`. |
| 46 | Recent sign-in: send `prompt=login` with `max_age`; no `auth_time` means not recent (plan:145) | Met | `providers.py:138` (`prompt=login` when `max_age=0`), `:176-180` (`authentication_time`); tests `test_recent_sign_in_parameters`, `test_authentication_time`, `test_auth_time_is_null_when_the_provider_gives_none`. |
| 47 | Clients cached per provider; one failed provider leaves others working; 30 s pause before retrying discovery (impl:131) | Met | `providers.py:24-25,113-122`; tests `test_one_provider_down_leaves_the_other_working`, `test_unreachable_discovery_waits_before_trying_again_then_recovers`. |
| 48 | `GET /auth/methods` returns `[{key, display_name, kind}]` plus `is_orcid` (impl:136,161) | Met | `auth.py:40-62`; `test_methods_lists_each_provider_without_its_settings`. |
| 49 | Until Task 5 adds registration, a non-ORCID provider signs in only people already in `user_identity` (impl:129) | Superseded | By Task 7: registration is now per provider (`resolve_sign_in(..., registration=provider.registration)`, `hub_login.py:167`). |
| 50 | `test_settings_check_names_users_stranded_by_removed_provider`, moved to Task 13's `auth` commands (impl:138) | Not met | No such command or warning in `app/cli/` (`grep stranded` empty). Task 13c is not merged to `main` (branch `feat/password-cli`); `app/cli/auth.py` has only `cleanup` and `end`. |
| 51 | `settings check` warns about the ORCID issuer mismatch between the list and `ORCID_ISSUER` | Met (extra) | `cli/settings.py:289`. |

### E. Linking and unlinking (Task 6)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 52 | A user's API key cannot change how that user signs in: link and unlink need a Hub session with a recent sign-in (plan:188, plan:282) | Met | `identities.py:41,69` (`require_recent_hub_session`), `auth_utils.py:701-705`; `test_api_key_and_stale_session_are_refused`. Device-code sessions also refused (`auth_utils.py:677-679`). |
| 53 | Link written only after the Hub confirms: start address from the API, browser sets the cookie, callback validates and writes nothing, confirm with client secret plus session token, row written only if code valid and unused, PKCE matches, the session's user started it, identity is free (plan:189-195, impl:322-325) | Met | `linking.py:76-107` (begin), `:110-129` (open, single use, 120 s), `:136-163` (code, no row), `:183-199` (`_link_grant`: PKCE, client, family, recent sign-in), `:211-239` (confirm). Tests `test_victim_opening_attackers_link_address_writes_nothing`, `test_attackers_login_cannot_be_linked_to_victims_account`, `test_link_callback_without_the_browser_cookie_refused`, `test_start_address_is_single_use`, `test_link_code_is_single_use_and_cannot_sign_in`, `test_sign_in_code_cannot_confirm_a_link`, `test_only_the_session_that_started_a_link_can_confirm_it`, `test_confirming_needs_a_recent_sign_in_again`. |
| 54 | Link callback requires `auth_time` no older than the link's start, refuses a missing one (impl:314) | Met | `linking.py:139-141`; `test_provider_must_have_asked_to_sign_in_again`. |
| 55 | Preview step so the Hub's confirm page names the login (impl:312) | Met | `linking.py:202-208`, `LinkPreviewRead` statuses; `test_preview_names_the_status`. |
| 56 | If the identity belongs to someone else, refuse; no merging ever (plan:199) | Met | `linking.py:166-176,221-227`; `test_second_orcid_and_another_persons_unique_id_are_refused`, `test_orcid_of_another_person_is_refused_for_someone_without_an_orcid`; registration test `test_approve_and_reject_only_waiting_accounts:189-190` shows a fresh account rather than a merge. |
| 57 | Unlinking locks the user row; refused if it would leave no way to sign in; ends the user's other Hub sessions (plan:200, impl:328) | Partly met | Lock: `identities.py:75` (`for_update=True`), `:120`. Sessions: `linking.py:275`. Last-method rule counts only `user_identity` rows (`linking.py:260-261,272-273`), so a password does not count as a way to sign in. See mismatch M1 (reproduced). |
| 58 | Every link and unlink writes an `auth_event` (plan:201) | Met | `linking.py:234,278`; asserted in `test_link_adds_a_verified_login_after_a_preview:116` and `test_unlink_ends_other_sessions_keeps_this_one_and_voids_live_links:265`. |
| 59 | Administrators link on someone's behalf by exact issuer and subject; service accounts refused (plan:202, impl:327) | Superseded | Decision 2026-11-06 (impl:346): `POST /users/{ref}/identities` removed; `test_administrators_cannot_add_a_login_to_an_account`. Admin list and remove remain (`identities.py:104-127`), service accounts refused there (`:98-99`). |
| 60 | Verifying your own ORCID runs the linking flow: same iD sets `verified_at`, a different one is refused and changes nothing (plan:205, impl:312) | Met | `linking.py:169` (`ALREADY_YOURS`), `:171-172` (`SECOND_ORCID`), `:229`; `test_linking_an_unverified_orcid_verifies_it`, `test_second_orcid_and_another_persons_unique_id_are_refused`. |
| 61 | Linking and unlinking void a live password link (plan:88) | Met | `linking.py:235,279` (`void_links`); `test_unlink_ends_other_sessions_keeps_this_one_and_voids_live_links`. |
| 62 | One login cookie per transaction; link code lasts 10 minutes; start address 2 minutes; at most 5 starts in 10 minutes (impl:314, clients design "Linking") | Met | `hub_auth.py:270-273` (`login_cookie_name(state)`), `linking.py:22-26`; `test_parallel_sign_ins_keep_their_own_cookies`, `test_start_address_expires_after_two_minutes`, `test_link_starts_are_limited`. |
| 63 | `identity move` moves a login only off an account that holds nothing, then disables that account; recorded in the audit log (impl:314,340,346) | Met | `linking.py:306-375` (`holdings`, `move_identity`), `cli/identity.py`; `test_move_takes_a_login_off_an_empty_account_and_switches_it_off`, `test_move_refuses_an_account_that_holds_anything`, `test_move_refuses_a_second_orcid_and_unknown_people`. |
| 64 | The Hub's unlink confirmation says a removed login from an `open` provider creates a new account (impl:314) | Met on the API side | Route docstring `identities.py:66-68`; the Hub text is the Hub's. |

### F. Protecting human administrators from API keys (Task 6b, plan table 271-284, decision 20)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 65 | Issue a link, approve, disable, enable: admin API key works and is logged; Hub session needs a recent sign-in (plan:273) | Partly met | Disable and enable: `users.py:681` (`require_recent_authentication`). Approve and reject: `_pending_account` (`users.py:685-690`) checks only `require_platform_admin`; a Hub session with a sign-in hours old is accepted. See mismatch M2 (reproduced). Logging: `registration.py:40,54`. |
| 66 | Refused with an API key for any human administrator target, including the key's own account: links, disable, enable, admin status, reading or regenerating keys, link or unlink (plan:274-278, impl:350-357) | Met | `require_human_admin_safeguard` (`auth_utils.py:691-698`) on `users.py:604,643,680`, `identities.py:100`, `passwords.py:59`, `api_keys.py:136`. Tests `tests/test_protect_human_admins.py` (all 9), `test_api_key_cannot_change_a_human_administrator`, `test_admin_removal_needs_a_recent_hub_session`. |
| 67 | Decision 20: an API key cannot edit another human administrator through `PATCH /users/{ref}` or revoke all their keys; people editing themselves stay unrestricted (plan:388) | Met | `users.py:603-604` (`if not auth_res.is_user(user)`), `:778`; `test_api_key_cannot_edit_or_revoke_the_keys_of_another_admin`, `test_recent_hub_session_can_edit_another_admin`. |
| 68 | `python -m app.cli admin set-role` keeps the last active human administrator (impl:352,357) | Met | `cli/admin.py:201-243`; `test_cli_set_role_changes_roles_and_keeps_the_last_admin`. |
| 69 | A role change records `role_changed` and drops the target's cached keys (impl:357) | Met | `users.py:660-662`; `test_role_change_is_recorded_and_drops_cached_keys`, `test_demoted_admin_key_loses_admin_rights_at_once`. |
| 70 | Revoking keys stays open to admin keys; service account roles unchanged (plan:279, impl:352) | Met (narrowed by decision 20 for admin targets) | `users.py:764-785`; `test_api_key_still_manages_ordinary_users_and_service_accounts`. `test_api_key_can_still_revoke_an_admins_keys` (impl:354) is superseded by decision 20. |

### G. Registration (Task 7, Task 13c password registration)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 71 | `CRUCIBLE_AUTH_REGISTRATION` sets the deployment default (`closed`); each provider can override (plan:168, impl:19,150) | Superseded | Task 4 decision (impl:128): every provider field, `registration` included, is required, so there is no deployment default to override (`config.py:226`); passwords have their own `CRUCIBLE_AUTH_PASSWORD_REGISTRATION` (`config.py:321`). `test_provider_override_beats_deployment_setting` and `test_implicit_orcid_defaults_open` are therefore moot. The plan text was not updated; see section 4. |
| 72 | `closed`: only an account an administrator created or linked; `approval`: anyone registers and waits; `open`: works immediately (plan:170-174) | Met | `sign_in.py:63-71`; `test_closed_refuses_someone_new_and_creates_nothing`, `test_approval_creates_a_waiting_account_with_its_login`, `test_open_creates_a_working_account`. |
| 73 | The older browser flow (`/auth/login`, `/auth/apikey`) applies the ORCID provider's registration | Partly met | `auth.py:94`: `registration = orcid_provider.registration if orcid_provider is not None else "open"`. With `ORCID_CLIENT_ID` set but no ORCID entry in the list, strangers register freely. See mismatch M3. |
| 74 | The Foundry's and the bundle's ORCID entries state `open` (plan:176) | Met | `deploy/compose/auth-providers.example.json` (`"registration": "open"`); `test_compose_bundle_lists_orcid_explicitly`. |
| 75 | Password registration only with `approval`; startup refuses `open`; form takes username, names and a password under the password rules, with the per-address limit (plan:177) | Met, with one addition | `config.py:672-673`; `password_registration.py:28-75`; `passwords.py:345-410`; tests `test_open_registration_with_passwords_is_refused_at_startup`, `test_asking_for_an_account_creates_one_that_waits_for_approval`, `test_each_rule_applies_and_keeps_what_was_typed`, `test_every_submission_counts_against_the_address`. The form also requires an email (`password_register_form.html:48`, `password_registration.py:47-50`), which the plan does not list; recorded only in `CHANGELOG.md:11`. See section 4. |
| 76 | A pending person sees "waiting" only after proving who they are; otherwise it looks like any failure (plan:178) | Met | Provider: `hub_login.py:167-170` after `verified_identity`; password: `hub_auth.py:531-541` after `attempt_password`; `/auth/apikey`: `auth.py:97-98`. Tests `test_waiting_is_shown_only_after_a_valid_sign_in`, `test_hub_sign_in_shows_the_waiting_page_and_issues_no_code`, `test_legacy_browser_sign_in_shows_the_waiting_page`, `tests/test_auth_password_sign_in.py` (pending case). |
| 77 | Administrators see pending accounts (`pending` filter) and approve or reject; rejecting deletes; expiry after 30 days through the cleanup job (plan:179, impl:370) | Met | `users.py:212-214,696-722`; `registration.py:14,66-83`; `hub_auth_maintenance.py:72-74`; tests `test_approve_and_reject_only_waiting_accounts`, `test_switching_to_closed_keeps_waiting_accounts_reviewable`, `test_stale_waiting_accounts_expire_through_the_same_function`, `test_cleanup_job_reports_and_removes_stale_waiting_accounts`. |
| 78 | `reject_pending_user`: one transaction, locks the user, checks still pending, records the event, removes identity group link and group, deletes the user; 409 and no change if anything else points at them; never deletes an approved user (plan:180) | Met | `registration.py:28-63` (`with_for_update`, `holdings`, deletes); `test_reject_with_unexpected_references_changes_nothing`, `test_approve_and_reject_only_waiting_accounts` (409 `not_pending` after approval). |
| 79 | Pending accounts hidden from lookups and project membership (impl:361) | Met | `user_utils.py:245-268` (`_visible`), `users.py:359,364,370,404,426,448`; `test_waiting_accounts_are_hidden_from_everyone_but_administrators`. |
| 80 | Restricted usernames in code (`admin`, `root`, `system`, `support`, `security`, `crucible`, `hub`, `api`, `me`, fixed words under `/users/`), lookalike rule, applies to self-chosen names, administrators may still set one, generated names avoid it, existing holders keep theirs (plan:181) | Met | `user_utils.py:73-93,222-242`; `password_registration.py:51-54`; `users.py:606-607`; `tests/test_restricted_usernames.py` (all 6). |
| 81 | New accounts' default permissions unchanged (plan:182, decision 8) | Met | `sign_in.py:79` adds only the identity group as owner; `provision_user` adds nothing else. No dedicated test (`test_new_account_default_permissions_unchanged` missing). |

### H. Cleanup job (Task 8)

| # | Requirement (spec) | Verdict | Evidence |
|---|---|---|---|
| 82 | The cleanup job covers the new tables and is added to the bundle beside `storage-cleanup`, hourly, same hardening (plan:126, impl:391) | Met | `hub_auth_maintenance.py:49-75` (events, devices, password tokens, throttle rows, pending users); `deploy/compose/compose.yaml:127-130` (`auth-cleanup`); `deploy/compose/README.md:147`; `tests/test_auth_cleanup_job.py`, `tests/test_hub_auth_cleanup_job.py`. |
| 83 | Retention values from settings; zero or negative refused (plan:337, impl:390) | Met | `config.py:312` (`ge=1`); `test_zero_or_negative_retention_is_refused`, `test_retention_period_is_a_setting`. |
| 84 | Addresses and failures purged after 90 days (plan:114, impl:389) | Not met | Same as row 29; see M4. |

Tally of rows 1 to 84: Met 70 (rows marked "met with a different shape" or "met with one addition" counted as met); Partly met 6 (rows 21, 29, 34, 57, 65, 73); Not met 5 (rows 11, 30, 33, 50, 84); Superseded 3 (rows 49, 59, 71). Mismatches M1 and M2 are the partly-met rows 57 and 65; M3 is row 73; M4 is rows 29 and 84; M5 is row 30; M6 is row 11.

## 2. Confirmed mismatches, most important first

### M1. A person with a password and one provider login cannot remove that login

- Spec: "Unlinking ... is refused if it would leave no way to sign in" (plan:200); Task 6 test `test_unlink_allowed_with_password_or_other_identity` (impl:337); the API routes table says a user may "Link or unlink a login" from a Hub session (plan:282).
- Code: `linking.py:260-261` (`sign_in_methods` selects only `UserIdentity` rows) and `:272-273` (`if len(methods) <= 1: raise LastSignInMethod`). `LocalCredential` is never consulted. By contrast, `DELETE /users/{ref}/password` does count identities when removing your own password (`passwords.py:183-184`), so the two halves of the "last method" rule disagree.
- Reproduced: `scratchpad/task14/probe_identities/test_probe.py::test_person_with_password_and_one_login_cannot_unlink_that_login` (user with one ORCID row and a `local_credential` row; `unlink_identity` raises `LastSignInMethod`). Passed on the SQLite test engine.
- What a user sees: on the Hub's sign-in methods page, someone who set a password and wants to drop ORCID (or a lab login) gets 409 `last_sign_in_method` "This is your last way to sign in; add another first", although their password still works. An administrator removing that login gets the same 409.
- Confidence: high. Disproof attempted: searched for a password-aware count anywhere on the unlink path (route, `unlink_identity`, `sign_in_methods`); none.
- Suggested fix: in `unlink_identity`, treat a `LocalCredential` row (when `password_enabled`) as a method: `if len(methods) + (1 if session.get(LocalCredential, user.id) else 0) <= 1: raise LastSignInMethod`. Add the missing test `test_unlink_allowed_with_password_or_other_identity`. Consider whether a locked password (`locked_at` set) should count; the spec says a lock "leaves other methods working", so counting it is consistent.

### M2. Approving or rejecting a pending account does not need a recent sign-in from a Hub session

- Spec: credential table row "Issue a setup or reset link, approve, disable, enable: Admin key works, and is logged | Admin, recent sign-in" (plan:273). Disable and enable were fixed for this after the security review (impl:253, impl:357); approve and reject were not.
- Code: `users.py:685-690` (`_pending_account`: `require_platform_admin` only), used by `/approve` (`:696-707`) and `/reject` (`:710-722`). Compare `_account_to_switch` (`:673-682`) which calls `require_recent_authentication`.
- Reproduced: `test_probe.py::test_approve_and_reject_accept_a_stale_hub_session` (Hub session authenticated 3 hours ago: approve 200, reject 204).
- What a user sees: nothing wrong at the Hub, which is the point; a stale or hijacked Hub session can let a stranger's `approval` registration in, or delete a pending account, without the step-up that disable requires.
- Confidence: high for the code path. The only mitigation is the Hub-side `Crucible-Privilege-Mode: elevated` header, which is a mode switch, not a re-authentication.
- Suggested fix: add `require_recent_authentication(auth_res)` to `_pending_account`, as `_account_to_switch` does, and a test parallel to `test_stale_hub_session_gets_a_step_up_for_a_human_administrator`.

### M3. The older ORCID browser flow registers strangers when no ORCID provider is listed

- Spec: `CRUCIBLE_AUTH_REGISTRATION` "defaults to `closed`" (impl:19); "`closed` (default): only an account an administrator created, or a login an administrator linked" (plan:172).
- Code: `auth.py:93-95`: `registration = orcid_provider.registration if orcid_provider is not None else "open"`. The older flow (`/auth/login`, `/auth/callback`, `/auth/apikey`, `/user_apikey`) runs whenever `ORCID_CLIENT_ID` and secret are set (`providers.py:97-102`), independent of `CRUCIBLE_AUTH_PROVIDERS`.
- What a user sees: a lab that sets `ORCID_CLIENT_ID` for the key page but lists only its own OIDC provider (`closed`) in `CRUCIBLE_AUTH_PROVIDERS` lets any ORCID holder create an account through `/user_apikey`. The Foundry is unaffected (its ORCID entry is `open`).
- Confidence: medium. Disproof attempted: no check elsewhere ties the older flow to the list; `test_apikey_page_writes_the_identity_row_with_the_older_flows_issuer` and `test_no_provider_is_implied_by_the_older_orcid_settings` both run with no list and rely on this fallback, so it looks deliberate for test convenience rather than decided. No decision in the spec names it.
- Suggested fix: default to `closed` when no ORCID provider is listed, or require an ORCID entry in the list whenever `ORCID_CLIENT_ID` is set (a `settings check` error), and adjust the two tests to list the ORCID provider.

### M4. Sign-in history: addresses and failures are kept for a year, not 90 days

- Spec: "Retention is a setting: addresses and failures 90 days, the rest one year" (plan:114); Task 8 test `test_cleanup_purges_auth_event_addresses_and_failures_after_90_days` (impl:389).
- Code: one setting `event_retention_days` (`config.py:312`, default 365) applied to all rows (`hub_auth_maintenance.py:49-56`). No column-level purge of `client_address`, no shorter window for `sign_in_failed`, `provider_refused` or `locked`. `security_audit_2026_11.md:711` notes the raw `client_address` and the 365-day retention as "reasonable", which is an observation, not a recorded decision to drop the 90-day rule.
- What a user sees: an administrator's history keeps client addresses for a year; a lab's data retention statement based on the plan would be wrong.
- Confidence: high.
- Suggested fix: a second setting (for example `CRUCIBLE_AUTH_ADDRESS_RETENTION_DAYS`, default 90) and, in the cleanup job, `UPDATE auth_event SET client_address = NULL WHERE at < cutoff90 AND client_address IS NOT NULL` plus `DELETE ... WHERE event IN (failure events) AND at < cutoff90`. Add the Task 8 test. Alternatively record a decision that one retention is enough and update plan:114.

### M5. No route exposes the sign-in history

- Spec: admin route `GET /users/{user}/auth-events` (plan:291); "the Hub shows the list under the user's sign-in methods" (plan:201); "an administrator needs to see this in the Hub" (plan:114).
- Code: `auth_event` has writers only (`events.py`, `passwords.py:113,188`, `users.py`); no reader in `app/api/routes/`. `CHANGELOG.md:34` announces only the table.
- What a user sees: administrators and users cannot see link, unlink, disable or failed sign-in history anywhere but the database.
- Confidence: high.
- Suggested fix: add the paginated admin route (and, for the Hub's account page, a self route such as `GET /account/auth-events` limited to the caller's rows), never returning `client_address` to non-administrators. Or record that the route is deferred and remove it from plan:291.

### M6. `POST /users` does not record the administrator in `created_by_user_id`

- Spec: plan:206 ("the row records them in `created_by_user_id`"), impl:291.
- Code: `users.py:573-577` and `user_utils.py:187` pass no `created_by`. Already recorded as open item 1.11 (impl:253). Confidence: high. Fix: add a `created_by: User | None` parameter to `provision_user` and pass `caller_row(session, auth_res)` from `POST /users`; `admin create` passes `None`.

### Not mismatches, but not yet in `main`

- `auth rewrite-issuer` (plan:60, impl:553), `auth revoke-passwords`, and the stranded-user check (impl:138) are absent from `main` (`app/cli/auth.py` has `cleanup` and `end`). The working branch `feat/password-cli` holds them; they should be reviewed when merged. Task 5a stages B4b to E (MFID for everyone) are recorded as unbuilt (impl:231) and the code still gives new ORCID users their ORCID as `unique_id`.

## 3. Missing tests

Against the plan's "Tests that must exist" (plan:335-351) and the task lists, for my area:

| Required test | Status |
|---|---|
| OIDC: wrong `iss`, `aud`, `nonce`, expired, unknown key; `none` and HS256; callback on wrong provider; `state` from another browser; missing `auth_time`; ORCID-shaped `sub` from another provider | Present (`test_wrong_tokens_are_refused`, `test_token_signed_with_another_key_is_refused`, `test_hs256_signed_with_the_client_secret_is_refused`, `test_unsafe_token_algorithms_are_refused_even_when_advertised`, `test_callback_for_other_provider_refused`, `test_state_from_other_browser_refused`, `test_auth_time_is_null_when_the_provider_gives_none`, `test_orcid_shaped_sub_from_other_provider_gets_no_orcid_treatment`). |
| OIDC: `subject_is_orcid` refused on another issuer | Superseded; covered as `test_old_subject_is_orcid_field_is_refused`, `test_orcid_status_comes_only_from_the_exact_issuer`. |
| Linking: identity already taken by row or `unique_id`; no email matching; attacker's link address; API keys refused; last method cannot be unlinked | Present (`test_second_orcid_and_another_persons_unique_id_are_refused`, `test_no_match_by_email_when_emails_equal`, `test_victim_opening_attackers_link_address_writes_nothing`, `test_api_key_and_stale_session_are_refused`, `test_last_method_cannot_be_unlinked`). |
| Linking: `test_unlink_allowed_with_password_or_other_identity` (impl:337) | Missing. Its absence hid M1. |
| Linking: an unlinked ORCID cannot sign back in, including after the password is also removed (plan:345; `test_unlinked_orcid_stays_refused_after_password_removed`, impl:299) | Partly: `test_unlinked_orcid_is_refused`, `test_apikey_page_refuses_an_unlinked_orcid`, `test_unlinked_orcid_at_the_hub_is_access_denied` exist; the "after the password is removed" variant is missing. |
| Linking: `test_no_merge_ever`, `test_every_link_and_unlink_writes_event` (impl:336-338) | Covered inside other tests (`test_approve_and_reject_only_waiting_accounts:189`, `test_link_adds_a_verified_login_after_a_preview:116`, `test_unlink_ends_other_sessions...:265`), not as named tests. |
| Linking: `test_admin_link_records_actor`, `test_admin_link_refuses_service_account` | Superseded (admin linking removed); `test_administrators_cannot_add_a_login_to_an_account` exists. |
| Disable: every method, handoff codes, Hub refresh, Hub access tokens, API keys | Present (`tests/test_auth_disable.py`; password method in `test_auth_password_sign_in.py::test_all_failures...[disabled]`). |
| Registration: each setting and provider override | Present for each setting; "provider override" moot (row 71). |
| Registration: `open` for passwords refused at startup; pending gets no session or key; waiting message only after proof; pending accounts expire | Present (`test_open_registration_with_passwords_is_refused_at_startup`, `test_waiting_accounts_get_no_hub_tokens_or_api_key`, `test_waiting_is_shown_only_after_a_valid_sign_in`, `test_stale_waiting_accounts_expire_through_the_same_function`). |
| Registration: `test_implicit_orcid_defaults_open` | Superseded (no implicit provider). |
| Registration: `test_approve_and_reject_cannot_both_win` (Postgres, impl:379) | Missing. The lock in `_locked_pending` makes it likely correct, but it is untested. |
| Registration: `test_new_account_default_permissions_unchanged` (impl:372) | Missing. |
| Registration: `test_reject_frees_username_and_login`, `test_reject_removes_user_identity_group_and_link` | Covered inside `test_approve_and_reject_only_waiting_accounts:183-190`, not as named tests. |
| Registration: `test_registration_refuses_a_restricted_username` | Covered by `test_choosing_your_own_restricted_username_is_refused` and `test_each_rule_applies_and_keeps_what_was_typed`. |
| Credential table: approve with a stale Hub session | Missing; would have caught M2. |
| Task 3: `test_admin_create_with_orcid_writes_identity_row` | Missing as a positive assertion (`test_orcid_becomes_the_identifier` checks only `unique_id`; `test_admin_create_refuses_an_orcid_linked_to_another_user` checks the refusal). |
| Task 5: `test_entered_orcid_is_not_verified`, `test_person_can_relink_their_own_unlinked_orcid`, `test_post_users_still_discovers_projects_and_records_the_admin` | `verified_at is None` for an entered ORCID is asserted in `test_auth_identity.py:46` and `test_user_orcid_field.py`; the relink test is missing; "records the admin" is missing (M6). |
| Task 8: `test_cleanup_purges_auth_event_addresses_and_failures_after_90_days`, `test_cleanup_keeps_credential_locks` | Missing (M4; the lock test belongs with Task 11 but is not present under any name). |
| Task 4: `test_settings_check_names_users_stranded_by_removed_provider` | Missing (row 50). |
| Foundry: existing ORCID user signs in, keeps `unique_id`, `/auth/apikey` works before and after the backfill | Present (`test_foundry_user_signs_in_and_keeps_unique_id_after_backfill`, `test_flag_off_keeps_looking_up_unique_id`). |

## 4. Where the spec is unclear or contradicts a later decision

1. `CRUCIBLE_AUTH_REGISTRATION` (plan:168, impl:19,150) no longer exists: the Task 4 decision made `registration` a required field of every provider entry, and passwords got `CRUCIBLE_AUTH_PASSWORD_REGISTRATION`. The plan's Registration section and Global Constraints still describe a deployment default with overrides. The code is consistent with the later decision; the plan should say so, and the Task 7 tests about overrides should be struck.
2. Password registration requires an email (`password_register_form.html:48`, `password_registration.py:47-50`), while plan:177 lists "a username, names and a password" and decision 15 says email is not part of this phase. CHANGELOG:11 records "a required but unverified email", but no spec line does. Either is defensible (an administrator approving needs a way to reach the person); the decision should be written into the plan.
3. The plan's "Who is this person" step 3 (plan:151, "`unique_id` is the ORCID if `subject_is_orcid`") and the 2026-11-04 update (plan:409, "`unique_id` becomes an MFID for every person") both stand in the document; the code follows the former until Task 5a step 2 is built. The plan should mark step 3 as superseded-pending-5a to avoid a reviewer counting it either way.
4. `assert_active` (impl:293) and `SignInOutcome` (impl:286) were replaced by inline checks and `SignInRefused`/`AccountPending` (impl:278 records the latter, not the former). Harmless, but a reader looking for `assert_active` finds nothing.
5. plan:114 (90-day retention for addresses and failures) versus the single `CRUCIBLE_AUTH_EVENT_RETENTION_DAYS` in CHANGELOG:26 and `security_audit_2026_11.md:711`: no decision reconciles them (M4).
6. The admin-link route's removal (impl:346) left plan:202 and plan:291 (`POST /users/{user}/identities`) unchanged; plan:291 also lists `GET /users/{user}/auth-events`, which was never built (M5).
7. Decision 20 (plan:388) narrows plan:279 ("Revoke someone's API keys: Admin key works, on anyone") for human administrator targets; the table row was not updated.
8. plan:135 says `HUB_AUTH_ORCID_*` is retired, and the clients design later retires `HUB_AUTH_*` in favour of `CRUCIBLE_AUTH_CLIENTS` (with `HUB_AUTH_*` still accepted alone, `config.py:406-420,676-677`). Consistent in code; the plan's Hub-settings references are stale.

## 5. Probe

`scratchpad/task14/probe_identities/test_probe.py` (two tests, both pass, meaning both suspected behaviours were observed). Run from the checkout with:

```
PYTHONPATH=. uv run pytest -q -p tests.conftest --rootdir=. \
  -o cache_dir=<scratchpad>/task14/probe_identities/.cache \
  <scratchpad>/task14/probe_identities/test_probe.py
```

# Task 14 review 2: API key hashing, sign-in for any client, public clients, device flow, sessions

Reviewed: `main` checkout at `scratchpad/review-main` (HEAD 7f3f6f3), read-only. Specs: `spec/self_hosting_phase3_plan.md` (API keys, API keys and credential routes, decisions 5, 10, 12, 14, 17 to 20), `spec/self_hosting_phase3_implementation.md` (Task 9, Task 9b, "Order from 2026-11-06"), `spec/self_hosting_sign_in_clients_design.md` (whole), plus the recorded later decisions in `spec/security_audit_2026_11.md` (status table, 2026-11-09) and `CHANGELOG.md` Unreleased where they record a decision the spec files do not.

Method: every requirement listed, code read to the line, each suspected mismatch checked against middleware, dependencies, Authlib 1.9.5 source (`.venv/.../authlib`), settings validation and tests before being kept. Local runs: `uv run pytest tests/test_api_key_routes.py tests/test_api_key_hashing.py tests/test_auth_clients.py tests/test_auth_device_flow.py tests/test_account_sessions.py tests/test_hub_tokens.py tests/test_removed_key_routes.py` → 120 passed. One reproduction script, `scratchpad/task14/grace_repro.py` (sqlite, no network), for item 38.

Verdict counts: met 54, partly met 8, not met 2, superseded by a later recorded decision 6 (70 items).

## 1. Requirements and verdicts

### API key hashing (plan "API keys", Task 9, decisions 5, 10, 12, 17, 19, 20)

| # | Requirement (spec line, paraphrased) | Verdict | Evidence |
|---|---|---|---|
| 1 | `api_key` table: `id`, `user_id` FK, `key_hash` unique SHA-256, `key_last4`, `name`, `created_at`, `last_used_at`, `expires_at`, `revoked_at` (plan "API keys" table) | met | `app/models/core_models.py:396-409`; `app/auth/api_keys.py:24-25` (`sha256`); migration `20261106_c9e1a4b7d362_api_key.py` |
| 2 | Several keys per user, cap 10 active (plan) | met | `app/auth/api_keys.py:17,119-131` (row lock on user, count of `active()`); `tests/test_api_key_routes.py::test_key_cap_of_ten` |
| 3 | New keys start with `crucible_` (plan; impl. Task 9 "New keys get the crucible_ prefix in step 2") | partly met | Named keys: `app/auth/api_keys.py:15,128`. Keys still minted without the prefix by `add_apikey` (`app/utils/auth_utils.py:126-131`, `secrets.token_hex(32)`): `/auth/apikey` and `/auth/apikey/regenerate` (`app/api/routes/auth.py:126,257`), service account create and rotate (`app/api/routes/service_accounts.py:177,206`), `admin create` (`app/cli/admin.py:154`) |
| 4 | Older keys keep working; existing keys' hashes computed from stored values | met | `app/auth/api_keys.py:69-88` (adopt plain key on first use), backfill `migrations/data/20261106_backfill_api_keys.py`; `test_older_key_without_prefix_still_works_and_is_copied_on_first_use`, `test_backfill_copies_every_key_with_its_life_unchanged` |
| 5 | A key is shown once, `no-store` | met | `app/api/routes/api_keys.py:247-265`; `test_created_key_is_shown_once_with_its_prefix_and_works` |
| 6 | `POST /account/apikeys` (name, optional expiry): refused with an API key; browser or Hub session with recent sign-in | met | `app/api/routes/api_keys.py:250` → `require_recent_hub_session` (`app/utils/auth_utils.py:701-705`); `test_api_key_cannot_create_a_key_and_a_stale_session_signs_in_again` |
| 7 | `GET /account/apikeys` active only, `?state=inactive` for the rest; API key allowed | met | `app/api/routes/api_keys.py:204-205,217,268-275` |
| 8 | `DELETE /account/apikeys/{id}` with an API key | met | `app/api/routes/api_keys.py:278-286`; `test_revoked_key_stops_working_and_is_listed_as_inactive` |
| 9 | Admin `GET /users/{ref}/apikeys`, `DELETE /users/{ref}/apikeys/{id}` with an API key | met (with decision 20 extension) | `app/api/routes/api_keys.py:304-320`; another human administrator's key needs a Hub session (`:318`), consistent with decision 20; `test_administrators_list_and_revoke_one_key`, `test_an_api_key_cannot_revoke_another_administrators_key` |
| 10 | Admin revoke all `DELETE /users/{ref}/apikey` as today; decision 20: not for another human administrator with an API key | met | `app/api/routes/users.py:765-790` (`require_human_admin_safeguard`, `revoke_user_keys`, `forget_cached_keys`); `test_admin_revoke_all_stops_cached_named_keys_at_once`, `test_revoked_key_is_gone_from_both_tables` |
| 11 | List entry: `id`, `name`, `last4`, `status`, `created_at`, `last_used_at`, `expires_at`, never the key | met | `app/models/api_keys.py:349-358` (adds `revoked_at`, `older_routes`) |
| 12 | Expiry 365 days by default; at most a year | met | `app/auth/api_keys.py:14,52`; `app/api/routes/api_keys.py:252-253`; `test_expiry_must_be_in_the_next_year` |
| 13 | Expired and revoked keys kept for audit, never deleted | met | `app/auth/api_keys.py:134-149` (rows stay; the plain `apitoken` row is deleted by design); `test_inactive_list_shows_when_a_key_was_revoked`. Rows go only with the account (`ondelete=CASCADE`, pending-account rejection) |
| 14 | `auth_event` records which key by `api_key_id` (plan "API keys") | not met | `AuthEvent` has no `api_key_id` column (`app/models/core_models.py:369-384`); `record_event` has no such parameter (`app/auth/events.py:34`); `create_key`/`revoke_key` record only the user and actor (`app/auth/api_keys.py:130,144`) |
| 15 | `/account/apikey/regenerate` and `/users/{ref}/apikey/regenerate` stay for older clients (plan); deprecated, replacing only the older-route key (decision 17, impl. 2026-11-06) | superseded (audit status 2026-11-09, #471: removed, 410) | `app/api/routes/account.py:166-169`, `app/api/routes/users.py:475-484`, `app/api/removed.py`; `tests/test_removed_key_routes.py`. The browser `POST /auth/apikey/regenerate` still replaces only the plain-table key (`app/utils/auth_utils.py:111-118`, `replace_plain_keys`) |
| 16 | `GET /account/apikey` answers 404 with instructions in `detail` (plan, Task 9 step 3) | superseded (#471: 410 `route_removed`) | `app/api/routes/account.py:160-163`; `test_removed_key_routes_answer_gone_and_name_the_replacement` |
| 17 | `/auth/apikey` and `/user_apikey` become pages that add a key by POST and never replace one (plan, Task 9 step 3) | superseded for now (design decision 4, 2026-11-06; "Order from 2026-11-06": steps 3 and 4 wait for a nano-crucible release) | Not built: `GET /auth/apikey` still returns the existing plain key through `find_or_add_api_access` (`app/api/routes/auth.py:110-132`) |
| 18 | Drop `apitoken` and its plain column (Task 9 step 4) | superseded for now (same decision) | `ApiToken` still written on every older-route key (`app/utils/auth_utils.py:129`) |
| 19 | API key creation and revocation logged (decision 10) | met | `app/auth/api_keys.py:130,144` (`API_KEY_CREATED`, `API_KEY_REVOKED`) |
| 20 | Clear the auth cache by hash (impl. Task 9) | met | `app/utils/auth_utils.py:88-98`; `app/api/routes/api_keys.py:244`; `test_admin_revoke_all_stops_cached_named_keys_at_once` |
| 21 | `last_used_at` updates the new table | met | `app/services/activity.py:47`; `test_last_use_is_recorded_on_the_hashed_key` |
| 22 | `find_api_key(session, presented)` by `sha256(presented)`; invalid, expired, revoked refused alike with 401 | met | `app/auth/api_keys.py:83-88`; `app/utils/auth_utils.py:157-171`; `test_expired_key_is_refused_like_an_unknown_one` |
| 23 | `POST /users` creates no key | met | `test_post_users_creates_no_key` |
| 24 | Backfill parses both `token_created` formats, reports unreadable, copies past-year keys with past `expires_at`, insert-only, dry run default | met | `app/auth/api_keys.py:36-46`; `test_backfill_copies_every_key_with_its_life_unchanged`, `test_backfill_reports_an_unreadable_date` |
| 25 | Older-route keys and backfill named "Default key"; `PATCH /account/apikeys/{id}` renames; `older_routes` flag (decision 17, impl. 2026-11-06) | met | `app/auth/api_keys.py:16,76`; `app/api/routes/api_keys.py:211,289-301`; `test_keys_can_be_renamed_and_the_older_routes_key_is_marked`, `test_backfill_names_copies_and_earlier_unnamed_rows` |
| 26 | Key lists paginated like the API's other lists (decision 19) | met | `app/api/routes/api_keys.py:214-235`; `test_key_lists_come_in_pages_newest_first` |
| 27 | `api_key_status`, `/account/verify`, user list count a key still only in `apitoken` and describe a working key before an expired one | met | `app/auth/api_keys.py:91-116`; `test_status_counts_a_plain_key_not_yet_copied`, `test_status_prefers_a_working_key_over_a_newer_expired_one` |
| 28 | Credential table: regenerate or read another administrator's API key refused with an API key | superseded (#471) | Routes removed for every caller (item 15, 16) |
| 29 | While both tables exist, revoking and regenerating update both | met | `app/auth/api_keys.py:141-143,157-166`; `test_revoked_key_is_gone_from_both_tables`, `test_regenerated_key_is_gone_from_both_tables`; plus trigger migration `2b0ab3a4520a` |

### Sign-in for any client (design sections 1 to 8, decisions of 2026-11-06)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 30 | `CRUCIBLE_AUTH_CLIENTS` list, each with `id`, `display_name`, `type` confidential or public, exact `redirect_uris` (design 1) | met | `app/config.py:272-303` (`OAuthClient`, `extra="forbid"`), `:372-380` unique keys and ids; `tests/test_auth_clients.py::test_unsafe_client_entries_are_refused`, `::test_a_client_listed_twice_is_refused` |
| 31 | Each client has `grants`: which of `authorization_code`, `refresh_token`, `device_code` it may use; "Device authorization grant for public clients that list it" (design 1, 2) | not met | No `grants` field on `OAuthClient` (`app/config.py:278-282`); grants follow the type only: `OAuthClient.check_grant_type` (`app/auth/oauth_server.py:135-140`) gives every public client the device grant and every client `authorization_code` and `refresh_token` |
| 32 | Public clients may allow loopback `http://127.0.0.1/<path>` and `http://[::1]/<path>`, any port (design 1; RFC 8252 7.3) | met | `app/auth/clients.py:25-39`; `app/config.py:269,292-299` (loopback without port only for public); `test_public_loopback_redirects_accept_any_port_and_nothing_else` |
| 33 | Confidential clients: secret named in the secret store, compared in constant time | met | `OAUTH_{KEY}_CLIENT_SECRET` (`app/config.py:301-303`); `compare_digest` with dummy comparison for unknown clients (`app/auth/clients.py:95-111`; `app/auth/oauth_server.py:113-122`); `test_each_client_authenticates_with_its_own_secret` |
| 34 | Only the operator registers clients; no dynamic registration; no consent screen | met | No RFC 7591 route; public clients get an RFC 8252 8.6 confirmation page (`app/api/routes/hub_auth.py:127-145`), not consent |
| 35 | Hub is the first entry, built from `HUB_*` when the list is not set; current deployments change nothing | met | `app/auth/clients.py:42-51`; setting both refused at startup (`app/config.py:674-675`); `test_the_older_hub_settings_alone_give_todays_client`, `test_both_ways_of_naming_the_hub_are_refused` |
| 36 | A `nano-crucible` public client in the Compose bundle by default (decision 3) | met | `deploy/compose/compose.hub.yaml:15-19`; `deploy/compose/README.md:238` |
| 37 | Authorization code with PKCE S256, required for every client (design 2; RFC 7636, RFC 9700 2.1.1) | met | `PkceS256(required=True)`, S256 only (`app/auth/oauth_server.py:201-209,387`); `start_sign_in` refuses any other method (`app/api/routes/hub_auth.py:213-216`); verifier re-checked in `exchange_handoff_code` (`app/utils/hub_login.py:271-283`); Authlib `rfc7636/challenge.py:91-127`; `test_a_code_needs_its_verifier_and_redirect`, `test_public_client_exchanges_its_code_with_pkce_and_no_secret` |
| 38 | Refresh with rotation and family revocation on reuse, "as today"; for public clients this satisfies RFC 9700's "sender-constrained or rotation" (design 2; RFC 9700 4.14.2) | partly met | Rotation and revocation: `app/utils/hub_tokens.py:150-212`. Added 30 s grace (`:19,140-147`): a spent token of generation `newest-1` is accepted again and mints a new generation (`:184-200`), and spending a token marks its siblings spent, restarting their grace. Reproduced locally: `scratchpad/task14/grace_repro.py` → `rounds=6 family_revoked_at=None live_refresh_tokens=2`. Recorded only in `CHANGELOG.md` ("Changed"), not in the spec; audit L3 has no decision in the status table |
| 39 | Device grant for public clients that list it (design 2) | partly met | Public clients always have it, confidential never (`app/auth/oauth_server.py:138-139,271-273`); `test_only_public_clients_get_device_codes_and_the_approval_client_gets_no_tokens`. See item 31 |
| 40 | Not offered: implicit, password, client credentials (design 2; RFC 9700) | met | `check_response_type` code only, `check_grant_type` (`app/auth/oauth_server.py:132-140`); metadata lists only the three grants (`app/api/routes/oauth.py:130-132`); `grant_type=password` → `unsupported_grant_type` (`test_a_code_needs_its_verifier_and_redirect`) |
| 41 | Routes `GET /oauth/authorize`, `POST /oauth/token`, `POST /oauth/revoke`, `POST /oauth/device_authorization`, `GET`/`POST /oauth/device` (design 3) | met | `app/api/routes/oauth.py:47,62,77,91`; `app/api/routes/device.py:203,209` |
| 42 | `GET /.well-known/oauth-authorization-server` (RFC 8414) so clients find the routes from the API address alone (design 3) | partly met | Served at `{api}/.well-known/oauth-authorization-server` (`app/api/routes/oauth.py:114-137`, mounted under the API root). RFC 8414 3 puts the document for an issuer with a path at `{host}/.well-known/oauth-authorization-server{path}`; the docstring records the deviation ("under the API address, not the host root"). Content has every REQUIRED field; `test_metadata_offers_no_secret_for_public_clients`, `test_metadata_names_the_device_endpoint` |
| 43 | The authorization response carries `iss` (RFC 9207), on codes and errors (design 3) | met | `client_redirect` adds `iss` to every redirect (`app/utils/hub_login.py:239-250`), used by `create_handoff_code` (`:190`) and `hub_error_destination`; `authorization_response_iss_parameter_supported: true` in metadata; the API's own device page checks it (`app/api/routes/device.py:254-255`) |
| 44 | `/auth/hub/login`, `/token`, `/refresh`, `/revoke` stay as aliases on the same code (design 3) | met | `app/api/routes/hub_auth.py:174-193` (login → `start_sign_in`), `:400-447` (`_through_server` → same `authorization_server()`); `test_the_hub_routes_and_the_standard_route_share_one_server`, `tests/test_hub_provider_flow.py::test_token_and_refresh_responses_include_auth_time` (response shape) |
| 45 | Linking moves to `/oauth/link/*` without aliases (linking decision 2026-11-06) | met | `link_router = APIRouter(prefix="/oauth/link")` (`app/api/routes/hub_auth.py:37`); CHANGELOG "Changed" |
| 46 | Provider callbacks keep their addresses | met | `app/api/routes/hub_auth.py:315,327` |
| 47 | Access and refresh tokens opaque, stored as hashes, no JWT (design 4) | met | `app/utils/hub_tokens.py:73-92,120-128`; `tests/test_hub_tokens.py::test_tokens_are_stored_hashed` |
| 48 | Lifetimes per client type: confidential 15 min / 7 d idle / 30 d; public 15 min / 14 d / 90 d (decision 2) | met | `app/utils/hub_tokens.py:14-30`; `test_public_sessions_last_longer_and_their_codes_wait_for_the_confirmation` |
| 49 | Tables keep their names (`hubtokenfamily` ...) | met | `app/models/core_models.py:428` |
| 50 | Principal `credential` becomes `session` with `client_id`; recent-sign-in checks apply to any session (`auth_time` within 10 min); error codes `hub_session_required` and `insufficient_user_authentication` keep their names (design 4) | met | `app/utils/permissions.py:98,112-117`; `app/utils/auth_utils.py:209-221,672-705`; `test_standard_code_exchange_works_once` (`principal.client_id`) |
| 51 | "nano-crucible signed in through the device flow can create and revoke named keys, link logins and change administrators, with no Hub" (design 4) | superseded (later decision recorded in CHANGELOG "Added": device-code sessions are refused wherever a recent sign-in is needed, 403 `browser_sign_in_required`) | `app/utils/auth_utils.py:676-680`; `HubTokenFamily.grant_type` (migration `1226f8497119`); `test_a_device_session_works_but_cannot_end_sessions_or_create_keys`. The design text and the later decision contradict; see section 4 |
| 52 | `client_id` recorded on `auth_event` for sign-ins and token issue (design 4) | partly met | Sign-in and registration events carry `client_id` (`app/auth/sign_in.py:86-88`, `app/utils/hub_login.py:234`, `app/auth/events.py:34-44`); there is no token-issue event at all (`AuthEventType`, `app/auth/events.py:11-31`), so a device-flow or refresh token issue leaves no event |
| 53 | A later `GET /account/sessions` with revoke (design 4) | met | See items 63 to 64 |
| 54 | User code: 8 characters from `BCDFGHJKLMNPQRSTVWXZ` shown as `WDJB-MJHT`, CSPRNG; input ignores case, dashes and spaces; device code high entropy, stored as a hash, single use (design 5) | met | `app/auth/devices.py:227-261` (`secrets.choice`, keyed hash of the user code), `:243-244` device code SHA-256; single use by conditional `UPDATE ... RETURNING` (`app/auth/oauth_server.py:345-350`); `test_a_device_is_approved_in_a_browser_and_gets_its_session_once`, `test_codes_are_stored_only_as_hashes` |
| 55 | Lifetime 10 minutes; polling every 5 s; `slow_down` grows the interval by 5 s (design 5; RFC 8628 3.2, 3.5) | met | `app/auth/devices.py:229-231`; `app/auth/oauth_server.py:323-336`; Authlib `rfc8628/device_code.py:132-146` for `authorization_pending`, `slow_down`, `access_denied`, `expired_token`; tests above and `test_denying_ends_the_device_sign_in`, `test_an_expired_device_code_says_so` |
| 56 | Code entry rate limited per client address and across all addresses, using the Task 11 throttle table (design 5) | partly met | Per address only: `begin_code_attempt` (`app/auth/throttle.py:177-186`, `_count_address` `:115-131`, 10 free failures then delays capped at 1 minute) from `app/api/routes/device.py:212`; no global counter or ceiling on pending device rows; `test_wrong_codes_count_against_the_address_and_are_answered_alike` |
| 57 | The device page shows the client's display name, says a program asks to sign in as this person and to continue only if they started it; the person signs in through a provider on that page; approval only after a sign-in no older than 10 minutes (design 5; RFC 8628 5.4) | met | `app/templates/device_code.html:19`, `device_confirm.html:37-38`; `max_age=0` forced (`app/api/routes/device.py:226`); `FRESH_SIGN_IN` check (`:168,262-263`); the API signs in as its own internal client that can never receive tokens (`app/auth/clients.py:54-66`, `app/auth/oauth_server.py:128-129,136-137`) |
| 58 | The page asks the person to confirm the code even when it arrives pre-filled through `verification_uri_complete` (design 5) | partly met | `GET /oauth/device?user_code=` renders a form with a Continue button and no auto-submit (`app/api/routes/device.py:203-206`, template). But `POST /oauth/device` has no CSRF token, `Origin` or `Sec-Fetch-Site` check (`:209-230`), so a page on another site can auto-submit the code and skip the warning page (audit L5, no decision recorded). The approve page still names client, person, address and needs a click |
| 59 | Pending, disabled and rejected accounts get the same pages and errors as in the Hub flow (design 5) | met | The device page signs in through `/oauth/authorize` (`app/api/routes/device.py:224-227`), so `resolve_sign_in`, `AccountPending` and the pending page apply; token issue refuses through `_may_hold_hub_tokens` (`app/utils/hub_tokens.py:49-50,104-106`) |
| 60 | Adapter subclassing `AuthorizationServer` with `query_client`, `save_token`, `send_signal`, `create_oauth2_request`, `create_json_request`, `handle_response`; sync server run in the thread pool; Authlib pinned and upgraded from 1.9.1 (design 6, 7) | met | `app/auth/oauth_server.py:359-393`; routes `run_in_threadpool` (`app/api/routes/oauth.py:57,72,86`); `pyproject.toml:34` `authlib>=1.9.5,<2`, `uv.lock` 1.9.5 |
| 61 | Compatibility: the Hub keeps working unchanged, same routes, client ID and secret, same token responses (design Compatibility) | partly met | Routes, ID, response shape: items 44, 48, `tests/test_hub_provider_flow.py:505`. The Hub secret must now be 32 or more characters (`app/config.py:17,642-643`), recorded in CHANGELOG ("check yours before upgrading") but not in the spec |
| 62 | Scopes come later; sessions carry the person's full power (decision 1) | met | `get_allowed_scope` returns `""`, `_Credential.get_scope` `""` (`app/auth/oauth_server.py:107-108,155-156`) |
| 63 | Token endpoint: HTTP Basic for confidential clients, `none` for public, RFC 6749 3.2 parameter rules (repeated or empty parameters) | met | `AUTH_METHODS` (`app/auth/oauth_server.py:27,124-130`); `oauth_request` (`app/api/routes/oauth.py:28-36`); Authlib `authenticate_client.py:85-111`; `test_repeated_parameters_are_refused`, `test_public_client_cannot_use_the_hubs_secret_routes_or_a_secret` |
| 64 | Revocation (RFC 7009): client authenticates; unknown and other clients' tokens answered alike; ends the family | met | `app/auth/oauth_server.py:237-260`; `app/utils/hub_tokens.py:237-254`; `test_revocation_answers_alike_for_unknown_and_other_clients_tokens` |
| 65 | Codes, sessions and links stay with the client that started them; removing a client ends its sessions | met | `query_authorization_code` and `exchange_handoff_code` check `client_id` and `callback_uri` (`app/auth/oauth_server.py:180-181`, `app/utils/hub_login.py:280`); `hub_access_family` refuses unknown clients (`app/utils/hub_tokens.py:229-230`); `test_one_clients_code_and_session_are_useless_to_another`, `test_removing_a_client_ends_its_sessions_at_once` |
| 66 | Public client: confirmation page before the code is sent to a loopback redirect (RFC 8252 8.6; CHANGELOG "Added") | met | `_deliver` (`app/api/routes/hub_auth.py:127-145`), public codes last 5 minutes for it (`app/utils/hub_login.py:21-28`); no end-to-end test (section 3) |

### Sessions (OWASP ASVS V7; CHANGELOG "Added" entries for sessions and device-code sessions)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 67 | `GET /account/sessions` lists mine (never tokens, `current` marked); `DELETE /account/sessions/{id}` and `DELETE /account/sessions` (all but this one) need a recent sign-in and refuse API keys | met | `app/api/routes/sessions.py:352-379`; `app/auth/sessions.py:153-197`; `test_people_see_their_own_sessions_with_the_current_one_marked`, `test_ending_one_session_needs_a_recent_sign_in_and_stops_it_at_once`, `test_an_api_key_can_list_but_not_end_sessions`, `test_sign_out_everywhere_else_keeps_this_session_and_is_recorded` |
| 68 | Administrators list and end a person's sessions (`/users/{ref}/sessions`); another human administrator's need a Hub session with a recent sign-in | met | `app/api/routes/sessions.py:382-411`; `test_administrators_list_and_end_a_persons_sessions`, `test_another_human_administrators_sessions_need_a_recent_sign_in` |
| 69 | `auth end-sessions --user|--all`, dry run unless `--commit` | met | `app/cli/auth.py:20-52`; `app/auth/sessions.py:200-207`; `test_the_command_ends_one_persons_or_everyones_sessions` |
| 70 | Sessions approved with a device code are recorded and refused wherever a recent sign-in is needed (keys, ending sessions, logins, administrators), 403 `browser_sign_in_required` | met | `grant_type` on the family (`app/utils/hub_tokens.py:21,116`, `app/auth/oauth_server.py:352`), `via_device` on the principal (`app/utils/auth_utils.py:219-220`), refused in `require_recent_authentication` which every `require_recent_hub_session`, `require_human_admin_safeguard` and `require_credential_change` path reaches (`:673-717`); `test_a_device_session_works_but_cannot_end_sessions_or_create_keys` |

## 2. Confirmed mismatches, most important first

1. **Refresh-token grace lets a copied refresh token be used indefinitely beside the owner's, so reuse is never detected** (item 38; audit L3 still open). Spec: design section 2, "Refresh with rotation and family revocation on reuse, as today. For public clients this satisfies RFC 9700's 'sender-constrained or rotation' rule." Code: `app/utils/hub_tokens.py:140-147` accepts a spent token of generation `newest-1` for 30 s; `:184-200` mints a fresh generation for it and, when a token is spent, marks its siblings spent at that moment, which restarts their grace. Reproduction (`scratchpad/task14/grace_repro.py`, sqlite, current main): a thief who refreshes a few seconds before the owner's 14-minute cadence keeps a live refresh token through six rounds; the family is never revoked and two live refresh tokens remain. What a user sees: nothing; the stolen CLI session on a shared cluster keeps working as long as the owner keeps using theirs. Confidence: high (reproduced). Suggested fix: on reuse within the grace, return the successor already issued (idempotent retry) instead of a new generation, and do not reset `spent_at` grace for siblings; or allow one grace reuse per family. Then record the grace in the spec.

2. **`POST /oauth/device` accepts cross-site form posts, so the code-entry confirmation can be skipped** (item 58; audit L5 still open). Spec: design section 5, "The page asks the person to confirm the code even when it arrives pre-filled through `verification_uri_complete`." Code: `app/api/routes/device.py:209-230` takes `user_code` from any POST without a CSRF token or `Origin`/`Sec-Fetch-Site` check and redirects into sign-in with `max_age=0`. A phisher's page can auto-submit the victim's browser past the "Never enter a code someone sent you" warning (`app/templates/device_code.html:19`); the approve page still needs a click and names the client, person, time and requesting address, so this shortens the attack rather than completing it. Confidence: high by reading (the suite's own `approve_in_browser` posts without any origin and gets 303). Suggested fix: refuse unless `Sec-Fetch-Site` is `same-origin`/`none` or `Origin` matches the public API address, or bind the POST to a nonce set by the GET.

3. **No per-client `grants`; every public client gets the device grant** (items 31, 39). Spec: design section 1 lists `grants` per client and section 2 says "Device authorization grant for public clients that list it." Code: `app/config.py:278-282` has no such field; `app/auth/oauth_server.py:135-140` derives grants from `type`. An operator who registers a public client for loopback sign-in only cannot keep device codes (the phishable path) away from it. Confidence: high. Suggested fix: optional `grants` on `OAuthClient` defaulting to today's behaviour, checked in `check_grant_type` and `DeviceAuthorization.create_endpoint_response`; or record the simplification as a decision.

4. **Device-code entry has no cross-address limit** (item 56). Spec: design section 5, "rate limited per client address and across all addresses." Code: `app/auth/throttle.py:177-186` counts per address only; `app/api/routes/device.py:212`. With about 34.5 bits of user code and a 10-minute lifetime a distributed guess is still impractical, but there is no ceiling on pending device authorizations per client either (`app/auth/devices.py:264-277`), so the code space can be made denser by an attacker holding many pending codes. Confidence: high for the mismatch, low for exploitability. Suggested fix: a global counter in the same table (fixed key) with a generous threshold, and a cap on pending rows per client.

5. **`auth_event` does not record which key** (item 14). Spec: plan "API keys", "`auth_event` records which key by `api_key_id`." Code: `app/models/core_models.py:369-384`, `app/auth/events.py:34`, `app/auth/api_keys.py:130,144`. An administrator reading `GET /users/{ref}/auth-events` cannot tell which of several keys was created or revoked. Confidence: high. Suggested fix: nullable `api_key_id` column (migration) set by `create_key` and `revoke_key`; or drop the line from the spec.

6. **Keys from the older routes still lack the `crucible_` prefix** (item 3). Spec: "New keys start with `crucible_`, so secret scanners can recognize a leaked one." Code: `app/utils/auth_utils.py:126-131` (`token_hex(32)`) used by `/auth/apikey`, `/auth/apikey/regenerate` (`app/api/routes/auth.py:126,257`), service accounts (`app/api/routes/service_accounts.py:177,206`) and `admin create` (`app/cli/admin.py:154`). A key fetched from the Foundry's `/user_apikey` page today, or any service-account key, is unrecognizable to scanners. Confidence: high. Suggested fix: generate `KEY_PREFIX + token_urlsafe(32)` in `add_apikey` too (both tables accept any string); note that nano-crucible has no format check.

7. **`client_id` is not recorded for token issue** (item 52). Spec: design section 4, "`client_id` is recorded on `auth_event` for sign-ins and token issue." Code: no token-issue event type (`app/auth/events.py:11-31`); a device-flow session's creation leaves only the browser sign-in event of the internal approval client (`crucible-device-approval`), so the session list is the only place the real client appears. Confidence: high. Suggested fix: record a `session_started` event with the family's `client_id` in `issue_hub_tokens_in_session`, or drop "token issue" from the design.

8. **RFC 8414 location** (item 42). Spec: design section 3 names RFC 8414. Code: `app/api/routes/oauth.py:114-123` serves the metadata at `{issuer}/.well-known/oauth-authorization-server`, where `issuer` is `https://host/api/v3`; RFC 8414 section 3 expects `https://host/.well-known/oauth-authorization-server/api/v3`. A client using a generic RFC 8414 discovery library would not find it; nano-crucible, told the path, will. Confidence: high (docstring acknowledges). Suggested fix: also route the RFC form through the proxy (Caddy and the Foundry's nginx), or state the deviation in the design.

Not counted as mismatches after checking: Authlib's own `redirect_uri`, `code_verifier`, client-grant and client-binding checks all run (`rfc6749/grants/authorization_code.py` `validate_token_request`, `rfc7636/challenge.py:91-127`, `rfc8628/device_code.py:validate_token_request`), with the API's own re-check in `exchange_handoff_code`; the device approval client is refused every grant and auth method; `localhost` http redirects are accepted only as exact entries, which RFC 8252 7.3 discourages but allows; admin session termination for ordinary people needs no recent sign-in, matching the credential table's treatment of revoking keys.

## 3. Tests that must exist: coverage

Implementation doc Task 9 (names as written; "covered by" when the name differs):

| Spec test | Status |
|---|---|
| `test_new_key_is_written_to_both_tables`, `test_hash_is_sha256_of_key`, `test_plain_key_not_in_new_table` | covered by `test_new_key_is_written_to_both_tables_as_a_hash` (`tests/test_api_key_hashing.py:37-43`) |
| `test_revoked_key_is_gone_from_both_tables`, `test_regenerated_key_is_gone_from_both_tables` | present |
| `test_existing_key_still_authenticates_after_backfill` | partly: `test_backfill_copies_every_key_with_its_life_unchanged` checks rows, `test_older_key_without_prefix_still_works_and_is_copied_on_first_use` checks authentication without the backfill; no test authenticates a backfilled key |
| `test_expired_key_refused_identically` | covered by `test_expired_key_is_refused_like_an_unknown_one` |
| `test_revoked_key_refused_within_cache_ttl` | covered by `test_revoked_key_stops_working_and_is_listed_as_inactive` and `test_admin_revoke_all_stops_cached_named_keys_at_once` (same worker only; cross-worker TTL is by design) |
| `test_service_account_key_works` | missing: no test authenticates a service account's key through `ApiKey` (service-account tests use `_user()` principals directly) |
| `test_several_keys_per_user` | covered by `test_key_cap_of_ten` |
| `test_named_key_listed_with_prefix_only` | covered by `test_created_key_is_shown_once_with_its_prefix_and_works` (lists `last4`, the spec's later shape) |
| `test_last_used_updates_new_table` | covered by `test_last_use_is_recorded_on_the_hashed_key` |
| `test_invalid_key_is_401_never_a_redirect` | missing as an HTTP test; `test_expired_key_is_refused_like_an_unknown_one` checks the exception, `tests/test_hub_auth_errors.py::test_authentication_failures_are_private` covers Hub routes only |
| `test_key_cap_of_ten`, `test_api_key_cannot_create_a_key`, `test_post_users_creates_no_key` | present (second as `..._and_a_stale_session_signs_in_again`) |
| `test_new_key_has_prefix`, `test_older_key_without_prefix_still_works`, `test_list_never_contains_the_key`, `test_list_is_active_only_by_default` | covered by `test_created_key_is_shown_once_with_its_prefix_and_works`, `test_older_key_without_prefix_still_works_and_is_copied_on_first_use`, `test_revoked_key_stops_working_and_is_listed_as_inactive` |
| `test_expired_and_revoked_keys_are_kept` | partly: revoked keys appear in the inactive list; no test lists an expired key or asserts rows survive |
| Step 3: `test_apikey_page_never_shows_an_existing_key`, `test_post_adds_a_key_and_shows_it_once`, `test_post_leaves_existing_keys_working`, `test_account_apikey_is_404_with_instructions`, `test_email_prompt_page_still_works` | not built, by the 2026-11-06 order (step 3 waits); `test_account_apikey_is_404_with_instructions` superseded by `test_removed_key_routes_answer_gone_and_name_the_replacement` |

Plan "Tests that must exist" lines in this area: "Disable: ... Hub refresh, Hub access tokens and API keys are refused" present in `tests/test_auth_disable.py` (`test_disabled_refused_on_hub_refresh`, `..._on_hub_access_token`, `..._on_api_key`). Task 4's `test_token_response_shape_unchanged` and `test_refresh_response_includes_auth_time` are covered by `tests/test_hub_provider_flow.py::test_token_and_refresh_responses_include_auth_time` (`:505` asserts the exact key set).

The design doc has no test list. Tests missing for behaviour it specifies or the CHANGELOG records:

- Public client end to end: no test drives `GET /oauth/authorize` for `nano-crucible` with a run-time loopback port, through the provider or password sign-in, to the `hub_confirm_program.html` page and the final redirect carrying `code`, `state` and `iss` (`grep` for the template or its text in `tests/` finds nothing). `allows_redirect` is unit-tested only.
- Refresh grace: no test for the renewable-grace scenario (audit L3's reproduction was not kept); `scratchpad/task14/grace_repro.py` is a starting point.
- Device: no test that a cross-site `POST /oauth/device` is refused (behaviour absent); no test of a cross-address limit (absent); `test_only_public_clients_get_device_codes_...` accepts `invalid_client` or `unauthorized_client` for a confidential client, so the RFC 8628 error for that case is not pinned.
- `GET /users/{ref}/auth-events` showing `client_id` for a sign-in through a non-Hub client: not tested.
- Session listing after a client is removed from settings (item 65 covers tokens, not the list).

## 4. Spec unclear or contradicted by a later decision

- Design section 4 says a device-flow session "can create and revoke named keys, link logins and change administrators, with no Hub"; the later decision (CHANGELOG "Added", migration `1226f8497119`) refuses device-code sessions for all of those with 403 `browser_sign_in_required`. The code follows the later decision; the design text should be struck or dated. Consequence for the stated goal "a deployment must work without the Hub": on a browserless machine a person can still work with a device session but cannot create a key, link a login or change an administrator; those need a loopback sign-in or the server CLI. The design does not say so.
- The 30-second refresh grace appears only in the CHANGELOG. The design still says "family revocation on reuse, as today" and cites RFC 9700 4.14.2 compliance for public clients. One of them should change, and audit L3 needs a recorded decision (the status table of 2026-11-09 lists no low findings).
- Plan "API keys and credential routes": "Revoke someone's API keys: admin key works, on anyone, as today." Decision 20 (#450) narrows this for another human administrator's keys (revoke all), and the code also applies it to revoke-one (`app/api/routes/api_keys.py:318`). The table row was not updated.
- Plan: "`GET /account/apikey` answers 404 with instructions"; the audit decision (#471) made it 410 `route_removed`, and removed `POST /users/{ref}/apikey` entirely, while design decision 4 keeps `/auth/apikey` reading back a plain key until Task 9 step 3. The plan's Task 9 step 3 text (404, "add a key by POST") is now partly stale: 404 → 410 done, the POST page still to build.
- Design section 1 `grants` versus the built type-derived grants (mismatch 3): either the design drops `grants` or the code gains it.
- Design "Compatibility: same client ID and secret": the 32-character minimum on `HUB_AUTH_CLIENT_SECRET` (`app/config.py:642`) is a recorded CHANGELOG change the design does not mention.
- RFC 8414 location (mismatch 8): the design's own wording ("from the API address alone") chose the non-RFC path; worth stating explicitly.
- `app/auth/sessions.py:112-115` cites OWASP ASVS 5.0 V7.5.2, V7.4.5 and V7.1.2. I did not verify the numbering offline; the behaviours (view and end own sessions after re-authentication, administrators end a person's sessions, no session cap) match ASVS V7's requirements in substance.

# Task 14 review, area 3: passwords (Tasks 10 to 13)

Reviewed: the `review-main` checkout at 346a0b2 (main) plus `origin/feat/password-cli` (7f3f6f3) for the server commands. Spec: `spec/self_hosting_phase3_plan.md` (plan), `spec/self_hosting_phase3_implementation.md` (impl), `spec/self_hosting_sign_in_clients_design.md`. Paths below are relative to the checkout; `branch:` marks the pull request.

Checks run: `uv run pytest tests/test_auth_passwords.py tests/test_auth_throttle.py tests/test_auth_password_sign_in.py tests/test_auth_password_links.py tests/test_auth_credential_routes.py tests/test_auth_password_registration.py tests/test_auth_cleanup_job.py` (156 passed, SQLite); the branch's `tests/test_auth_password_cli.py` and `tests/test_auth_password_links.py` against an exported copy of the branch (26 passed); one reproduction test for mismatch M1 (below). Postgres and browser tests were not run (no `CRUCIBLE_TEST_PG_URL`, no browsers); they exist in the suite and run in CI.

Verdict counts: 72 requirements. Met 56, partly met 7, not met 3, superseded 6.

## 1. Requirement table

### Storage and pepper (plan 213-218, 64-75)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 1 | Argon2id through `pwdlib[argon2]` 0.3.1, pinned, PHC string, no custom format | met | `pyproject.toml:35`; `app/auth/passwords.py:14-16,42-43,84` |
| 2 | Parameters above OWASP floor m=19 MiB, t=2, p=1, stated in settings, test fails below floor | met | `app/config.py:19,315-317,360-363` (defaults 64 MiB, t=3; floor pairs per OWASP); `tests/test_auth_passwords.py:32,44` |
| 3 | Rehash at next successful sign-in when parameters change | met | `passwords.py:106-112` (`verify_and_update`); `throttle.py:362-363`; `hub_auth.py` via `attempt_password`; `tests/test_auth_throttle.py:367` |
| 4 | Pepper: NFC, HMAC-SHA256 with secret outside the DB, base64, then Argon2id, in that exact order | met | `passwords.py:69-73` matches the impl snippet (impl 459-462) |
| 5 | `pepper_id` stored, rotation at next sign-in via previous peppers | met | `passwords.py:58-66,111-112`; `config.py:319-320,348-358`; `tests/test_auth_passwords.py:91` |
| 6 | Empty pepper counts as missing; startup refuses it when passwords are on | met | `config.py:147-160` (`missing` treats empty SecretStr as missing), `400-402`; `tests/test_auth_passwords.py:102` |
| 7 | Pepper backup kept apart from DB backups, and the install guide says so | partly met | Said in `docs/hub_auth_operations.md:22`; `deploy/compose/README.md` and `docs/self_hosting_secrets.md` do not mention the pepper (Task 15 rewrites the guide; impl 586 plans it) |
| 8 | `local_credential` columns as in the table; separate from `user`; row only once a password is set; service accounts never, in every writing path, with a test | met | `app/models/core_models.py:322-334`; `passwords.py:144-151` is the only writer and raises `ServiceAccountPassword`; `tests/test_auth_passwords.py:195`, `test_auth_password_links.py:280` |
| 9 | Hash never in UserRead or any response schema | met | no `password_hash` outside `core_models.py`; `tests/test_auth_passwords.py:212` |

### Password rules (plan 221-228; decision 22)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 10 | At least 15 code points (setting, 12 lowest), accept up to 256 | met | `config.py:314` (ge=12, le=64, default 15); `passwords.py:22,129-134`; `tests/test_auth_passwords.py:141,146,155,160` |
| 11 | NFC before hashing; never trim, truncate or alter | met | `passwords.py:70,129`; `hub_auth.py:533` passes the raw form value; `tests/test_auth_passwords.py:60,65`, `test_auth_password_sign_in.py:216` |
| 12 | No composition rules, no scheduled expiry; forced change only on compromise via remove + link, `auth revoke-passwords` | met | `passwords.py:126-141` has no composition check; `app/api/routes/passwords.py:157-189`; branch `app/auth/passwords.py` `revoke_all_passwords`; `tests/test_auth_passwords.py:165` |
| 13 | Whole password compared case-insensitively against a shipped list filtered to the minimum length plus context (username, email, names, service name); reason given with guidance; offline | met | `passwords.py:116-141`; `app/auth/data/blocked_passwords.txt` header (SecLists Pwdb top 1M, MIT, 12+ code points since the minimum can be 12, 30,000 entries); `scripts/build_password_blocklist.py`; `tests/test_auth_passwords.py:169,183,189` |
| 14 | Password managers, autofill, paste work; correct `autocomplete`; no blocking script; show-password control | met | `app/templates/hub_password_form.html:44,47,49,54-58`, `password_link_form.html:101-108`, `password_register_form.html:170-179`; `tests/test_auth_password_sign_in.py:246`, `test_auth_password_browser.py` |
| 15 | No hints or security questions | met | none in templates or routes |
| 16 | Empty submitted password or missing stored hash never verifies | met | `passwords.py:97-99`; `tests/test_auth_passwords.py:72` |

### Attempts and device cookies (plan 230-241; impl 480-505)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 17 | Counted on the account; one atomic `UPDATE ... RETURNING` counts first, then hashing with no lock held; crash counts as failure | met | `throttle.py:292-332` (`begin_attempt` in a short transaction, `update(...).returning`), `398-408`; `tests/test_auth_throttle_postgres.py:60,86`, `test_auth_throttle.py:231` |
| 18 | Success accepted only if `password_changed_at` unchanged since the attempt began | met | `throttle.py:364-366`; `tests/test_auth_throttle.py:204` |
| 19 | Unknown browsers: 4 free failures, then 2, 4, 8 s ... up to 15 min; success resets both counters | met | `throttle.py:189,196-197,227-231,325-327,361`; `tests/test_auth_throttle.py:71,82,90` |
| 20 | During a delay: dummy work, password not checked, attempt not counted | met | `throttle.py:316-322,404-406`; `tests/test_auth_throttle.py:98` |
| 21 | Two counters: unknown limit 90 (only they are locked out), known browser or session limit 10, no delays, then password disabled until a new link; total never past 100 | met | `throttle.py:190-192,311-316,328-329`; `tests/test_auth_throttle.py:109,169,183,191`, `test_auth_throttle_postgres.py:73` |
| 22 | Correct password from a known browser clears the unknown-browser lockout | met | `throttle.py:361` (`failed_attempts=0, blocked_until=None`); `tests/test_auth_throttle.py:138` |
| 23 | Device cookie signed with `itsdangerous` under its own salt, new one at each sign-in, carries `password_changed_at` so a change cancels it; failures per account not per cookie | met | `throttle.py:199,247-266,298-299`; `hub_auth.py:552-553`; `tests/test_auth_throttle.py:145,154,204,215` |
| 24 | Per-address wider limit covering sign-in, registration, opening and submitting links | met | `throttle.py:193-198,273-289,335-343`; `passwords.py (routes):246,287,410`; `tests/test_auth_throttle.py:251,260`, `test_auth_password_links.py:245`, `test_auth_password_registration.py:119` |
| 25 | Later decision (audit M1): address delay capped at one minute, known browsers skip it | met | `throttle.py:198,287,300-301`; `tests/test_auth_throttle.py:260,270` |
| 26 | Address from the header the bundle's proxy sets, trusted only when configured (impl: `CRUCIBLE_AUTH_TRUSTED_PROXY_HOPS`) | partly met | Built on uvicorn's `ProxyHeadersMiddleware` with `FORWARDED_ALLOW_IPS` instead of a Crucible setting: `throttle.py:241-244`, `deploy/compose/compose.yaml:81` (`"*"`), `deploy/compose/README.md:330`. Safe only while Caddy (2.11.7, overwrites `X-Forwarded-For` from untrusted clients) is the sole route to `api:8000`. Test `tests/test_auth_throttle.py:328` exercises uvicorn, not the deployed chain; no startup check that passwords are on with `FORWARDED_ALLOW_IPS` unset (`docs/hub_auth_operations.md:24` warns instead). Acceptable native choice; see U3 |
| 27 | Address rows go when `last_failure_at` is 30 days old; table holds a keyed hash | met | `throttle.py:195,234-238,411-418`; `hub_auth_maintenance.py:67-71`; `tests/test_auth_throttle.py:297,304` |
| 28 | `POST /account/password` refused with an API key; session uses the known-browser counter | met | `passwords.py (routes):93` via `require_credential_change` (`auth_utils.py:708-717`), `:140` (`session_authenticated=True`); `tests/test_auth_credential_routes.py:116,139` |
| 29 | Limit on simultaneous hash checks per worker | met | `throttle.py:373-395`, `config.py:324`; `tests/test_auth_throttle.py:347` |
| 30 | Lock stays on the account across a rename, not passed to the next holder | met | counters live in `local_credential` keyed by `user_id`; `tests/test_auth_throttle.py:375` |
| 31 | `auth_event` records failure and lock; failure on an unknown name recorded without a user; `client_address` stored | not met | `AuthEventType.SIGN_IN_FAILED` and `LOCKED` (`app/auth/events.py:13,15`) have no caller anywhere in `app/`; `hub_auth.py:512-555` records nothing on failure; `throttle.py:328-331` sets `locked_at` without an event; `record_event` is never passed `client_address` on the password path. See M2 |

### Responses and the form (plan 243-254; impl 507-535)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 32 | One generic failure text for unknown user, wrong password, delayed, locked, disabled, no password, service account; same work; timing test | met | `hub_auth.py:446,475-489,529-537` (`HashBusy` also answers the same page); `tests/test_auth_password_sign_in.py:119,150` |
| 33 | Sign-in name is the lowercase unique username; case and surrounding spaces tolerated | met | `hub_auth.py:481-485`; `tests/test_auth_password_sign_in.py:216` |
| 34 | Jinja2 templates with autoescaping, no hand-written HTML | met | `hub_auth.py:50`; `app/templates/*.html` |
| 35 | Served under `/auth/hub/`; HTTPS unless localhost, refused at startup otherwise | superseded (routes) / met (HTTPS) | Later design puts the form on the generic page at `/oauth/password` (`spec/self_hosting_sign_in_clients_design.md:125`, CHANGELOG line 18); `config.py:665-671`; `tests/test_auth_password_sign_in.py:270` |
| 36 | POST only, tied to the login record plus browser cookie (CSRF); typo does not consume; consumed on success | met | `hub_login.py:196-236` (`peek_login`, `create_local_code`); `hub_auth.py:524-528,547`; `tests/test_auth_password_sign_in.py:178,200,209` |
| 37 | Form left open past 10 minutes says expired, offers a restart, is not counted | met | `hub_auth.py:526-528` returns before `attempt_password`; `hub_password_form.html:38`; `tests/test_auth_password_sign_in.py:186` |
| 38 | Headers `Cache-Control: no-store`, strict CSP with `frame-ancestors 'none'`, `Referrer-Policy: no-referrer` | met | `hub_auth.py:120-124,470-471`; `passwords.py (routes):205-207`; `main.py:60-73` adds them for `/oauth/`; browser CSP test `tests/test_auth_password_browser.py` |
| 39 | Password never reaches logs; 422 bodies on password routes hide inputs | met | `main.py:68-85`; `tests/test_auth_password_sign_in.py:167`, `test_auth_password_links.py:253` |
| 40 | Caddy's size and timeout limits move with the login routes; every form within the 8 KB limit | partly met | `deploy/compose/Caddyfile:14-23` caps only `/api/v3/auth/hub/*`; `/oauth/*` falls under the 10 MiB `handle_path` and the API's `BodySizeLimit` (`app/api/body_limit.py`). Form fields have `max_length` but Starlette reads the whole body first. Already logged in `spec/security_audit_2026_11.md:161`. See M4 |
| 41 | Device cookie set on success and read on the next attempt | met | `hub_auth.py:531,553`; `tests/test_auth_password_sign_in.py:238` |
| 42 | `auth_time` set so step-up works; `required_user` enforced | met | `hub_login.py:232`; `hub_auth.py:542-545`; `tests/test_auth_password_sign_in.py:94,231,264` |
| 43 | Pending account sees the waiting page only after a correct password | met | `hub_auth.py:539-541`; `tests/test_auth_password_sign_in.py:257` |
| 44 | Routes 404 when passwords are off (pages); impl "test_password_disabled_route_is_404" | met | `hub_auth.py:492-494`; `tests/test_auth_password_sign_in.py:223` |

### Setup, change and reset (plan 256-267, 77-88; impl 537-566)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 45 | `password_token` columns as in the table; SHA-256 of a 256-bit token; partial unique index for one live link per user | met | `core_models.py:337-355`; `migrations/versions/20261108_a7df9dd039cf_password_token.py:24`; `password_links.py:45-46,78`; `tests/test_auth_password_links_postgres.py:85,101` |
| 46 | New link, disabling, password change, or identity change voids the live one | met | `password_links.py:62-77`; `passwords.py:151`; `users.py:742`; `linking.py:235,279,373-374`; `tests/test_auth_password_links.py:177,204` |
| 47 | Admin gets a one-time link, 24 h default (later: 1 to 168 h setting); no temporary password | met | `config.py:322`; `passwords.py (routes):38-66`; `tests/test_auth_password_links.py:107,321` |
| 48 | Invites do not attach a provider login | met | `issue_link` takes no identity (`password_links.py:69`) |
| 49 | Form is a POST with CSRF; using a link locks its row so two tabs cannot both succeed | met | `passwords.py (routes):280-286` (nonce in cookie and signed form token); `password_links.py:120-121` (`with_for_update`); `tests/test_auth_password_links.py:224`, `test_auth_password_links_postgres.py:66` |
| 50 | Opening never consumes; first open moves the token into a short-lived cookie and redirects to an address without it | met | `passwords.py (routes):236-258` (303, 15-minute cookie, `path` scoped); `tests/test_auth_password_links.py:121` |
| 51 | `admin create` prints a link when passwords are on; `admin password-link` issues one; no default password, no open setup page | met | branch `app/cli/admin.py` `_run_create` (`password_link` in the JSON); `app/cli/admin.py:243-277` on main; branch `tests/test_auth_password_cli.py:54`; `tests/test_auth_password_links.py:298` |
| 52 | Using a link writes the password, consumes it, clears any lock, ends all Hub sessions, refuses earlier handoff codes, no automatic sign-in | met | `password_links.py:112-140` (also denies pending device approvals); `passwords.py (routes):303-305`; `tests/test_auth_password_links.py:133,149` |
| 53 | Changing needs the current password and a recent sign-in, ends other sessions | met | `passwords.py (routes):93,136-144,100-113` (`except_family_id`); `tests/test_auth_credential_routes.py:116` |
| 54 | First password needs a recent sign-in; a generated username (later decision: username must be set first, 409 `username_required`) | superseded, met as decided | `passwords.py (routes):134-135`; CHANGELOG line 12; `tests/test_auth_credential_routes.py:100,110` |
| 55 | Password events and API key events separate; a change never touches keys; revoking keys is `DELETE /users/{ref}/apikey` | met | no key writes in `passwords.py`, `password_links.py`, branch `revoke_all_passwords`; `tests/test_auth_credential_routes.py:149` |
| 56 | Password errors as FastAPI field errors; `current_password_invalid` 422 never 401 | met (later decision) | `passwords.py (routes):78-83,138,144,147`; `tests/test_auth_credential_routes.py:133` |
| 57 | 501 `feature_disabled` when passwords are off on `POST /account/password` and `POST /users/{ref}/password-link` | met (later decision) | `passwords.py (routes):73-75,52,131`; `app/api/errors.py`; `tests/test_auth_credential_routes.py:157`, `test_auth_password_links.py:289`. `DELETE /users/{ref}/password` has no such check, which is sensible (cleanup after switching passwords off) |
| 58 | Username required for anyone with a password; clearing refused; rename needs recent sign-in and is refused with an API key | partly met, rest superseded | Clearing refused: `app/utils/user_utils.py:123-128`, `account.py:120`; `tests/test_auth_password_sign_in.py:280`. Rename restrictions: dropped by the user's decision recorded at impl 357 (Task 6b: "changing username and email with an API key stays unrestricted ... revisit in Task 13 only if the user asks") |

### API keys and credential routes table (plan 269-284) as it applies to passwords

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 59 | Issue a link: admin key works for ordinary users, logged; refused for any human administrator including yourself; Hub session needs recent sign-in | met | `passwords.py (routes):50-59` (`require_platform_admin`, `require_recent_authentication`, `require_human_admin_safeguard`); `password_links.py:84` (`LINK_ISSUED`); `tests/test_auth_password_links.py:107,263` |
| 60 | Delete a human administrator's password: refused with a key; admin session with recent sign-in | met | `passwords.py (routes):176` via `require_credential_change` → `require_human_admin_safeguard` (`auth_utils.py:691-717`); `tests/test_auth_credential_routes.py:165,180,189` |
| 61 | Change own password / set a first password: key refused, device-code session refused, stale session gets step-up | met | `auth_utils.py:673-688,701-714`; `tests/test_auth_credential_routes.py:139` |
| 62 | Refusals carry stable codes `api_key_not_allowed`, `admin_target_not_allowed` (impl 552) | partly met | Codes are stable but named `hub_session_required` (both cases) and `browser_sign_in_required`; `tests/test_auth_credential_routes.py:147`, `test_auth_password_links.py:265`. See U1 |
| 63 | New admin routes accept `Crucible-Privilege-Mode` through `authorize_user` | met | both routes use `authorize_user` (`passwords.py (routes):46,163`); `tests/test_auth_password_links.py:34`, `test_auth_credential_routes.py:29` |
| 64 | Removing your own only way in is refused (unlink rule applied to passwords) | met | `passwords.py (routes):183-184`; `tests/test_auth_credential_routes.py:196` |

### API routes list (plan 288-292)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 65 | `POST /account/password`, `POST /users/{user}/password-link`, `DELETE /users/{user}/password`, `GET` and `POST` password form, password-link and register pages, all in the OpenAPI snapshot | met, paths superseded | Pages live under `/oauth/` (`hub_auth.py:444`, `passwords.py (routes):28`); `main.py:321-326`; the `/auth/hub/*` names in plan 292 and impl 550 are superseded by the sign-in clients design and CHANGELOG lines 11, 13, 18 |

### Registration with a password (plan 177-181; impl 564; CHANGELOG line 11)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 66 | Password registration only with `approval`; startup refuses `open` | met | `config.py:321,672-673`; `password_registration.py:177-181`; `tests/test_auth_password_registration.py:136` |
| 67 | Form takes username, names, password (later: required unverified email), same password rules, per-address limit, one shared answer for a username or email in use | met | `passwords.py (routes):386-430`; `password_registration.py:184-208` (`IN_USE`, 409), `:211-224` (`pending_at`, `REGISTERED`); `tests/test_auth_password_registration.py:63,89,104,119` |
| 68 | Pending account gets no session or key on any path | met | `register` issues nothing; `hub_auth.py:539-541` consumes the login and shows the waiting page; `tests/test_auth_password_registration.py:63` |
| 69 | Restricted usernames and the lookalike rule apply at registration | met | `password_registration.py:201-203` (`assert_self_chosen_username_allowed`); `tests/test_auth_password_registration.py:104` |
| 70 | Password links work with registration `closed` | met | `issue_link` and `use_link` never read the registration setting; test fixtures leave it `closed` (`tests/test_auth_password_links.py`) |

### Server commands (plan 224, 60, 263, 284; impl 553; branch)

| # | Requirement | Verdict | Evidence |
|---|---|---|---|
| 71 | `auth revoke-passwords [--commit]`: removes every password after a breach, dry run by default; API keys untouched | met | branch `app/auth/passwords.py` `revoke_all_passwords` (locks rows only on commit, voids links, revokes `HubTokenFamily`, records `PASSWORD_REMOVED`); branch `app/cli/auth.py:31-55`; branch `tests/test_auth_password_cli.py:67` |
| 72 | `auth rewrite-issuer --from --to [--commit]` for a provider whose issuer changed, dry run by default | met | branch `app/auth/identity.py` `rewrite_issuer` (refuses same issuer, unlisted target, any collision); branch `tests/test_auth_password_cli.py:90,105`. Note: rewriting onto an ORCID issuer could trip the one-ORCID partial index for a person who already has one; the command catches no `IntegrityError`, so that case fails loudly and changes nothing, which is acceptable |

## 2. Confirmed mismatches, most important first

### M1. A person with a password and one provider login cannot unlink that login (cross-area, verified)

- Spec: plan 200 "Unlinking ... is refused if it would leave no way to sign in"; impl 337 `test_unlink_allowed_with_password_or_other_identity`; plan 339 "lock leaves other methods working" rests on the password counting as a method.
- Code: `app/auth/linking.py:260-261` `sign_in_methods` selects only `UserIdentity` rows; `unlink_identity` at `:268-273` raises `LastSignInMethod` when `len(methods) <= 1`, never consulting `local_credential`. Contrast `sign_in_labels` (`linking.py:243-257`), which does count passwords, and `remove_user_password` (`passwords.py (routes):183`), which correctly treats identities as the other methods.
- Reproduced: a user with a stored password and one `lab` identity; `unlink_identity` raised `LastSignInMethod` (scratchpad `repro/test_repro.py`, 1 passed).
- What a user sees: `DELETE /account/identities/{id}` answers 409 `last_sign_in_method` although they can still sign in with their password; `DELETE /users/{ref}/identities/{id}` by an administrator fails the same way. A lab moving off a provider onto passwords cannot remove the old login.
- Fix: make the "last method" test count the password: `len(methods) + (session.get(LocalCredential, user.id) is not None) <= 1`, and add the missing test `test_unlink_allowed_with_password_or_other_identity`. Belongs to the linking area; reported here because the password side is what is miscounted. Confidence: high.

### M2. Password failures and locks write no `auth_event`

- Spec: plan 107 event list includes "failure, lock"; plan 114 "A failure on an unknown name is recorded without a user"; plan 112 `client_address`.
- Code: `AuthEventType.SIGN_IN_FAILED` and `LOCKED` (`app/auth/events.py:13,15`) are never recorded by any module (`grep -rn "AuthEventType.SIGN_IN_FAILED\|AuthEventType.LOCKED" app` finds nothing). `password_sign_in` (`hub_auth.py:512-555`) returns the failure page without an event; `begin_attempt` sets `locked_at` (`throttle.py:328-331`) silently. `record_event` is never given `client_address` (impl 278 deferred it to Task 11, which is built).
- What a user sees: an administrator reviewing `GET /users/{user}/auth-events` in the Hub, or the cleanup job's 90-day purge of "addresses and failures", has nothing to show for a guessing attack or for why someone is locked out. The password lock in particular is invisible until the person complains.
- Fix: in `password_sign_in` after a refusal, record `SIGN_IN_FAILED` in its own short transaction with `user` when the username resolved and `client_address`; in `begin_attempt` (or `attempt_password`), record `LOCKED` when `after["locked_at"]` is set. Keep the username out of the event, as plan 114 requires. Confidence: high.

### M3. The 90-day retention for addresses and failures does not exist

- Spec: plan 114 "Retention is a setting: addresses and failures 90 days, the rest one year"; plan 337 and impl 389 `test_cleanup_purges_auth_event_addresses_and_failures_after_90_days`.
- Code: `hub_auth_maintenance.py:49-56` applies one `event_retention_days` (365) to all events; there is no second setting and no purge of `client_address`. Partly a consequence of M2 (nothing is written to purge), but the setting and the test are still absent. Shared with the identities area, which owns `auth_event`.
- Fix: add `CRUCIBLE_AUTH_FAILURE_RETENTION_DAYS` (90), null out `client_address` and delete `sign_in_failed`/`locked` rows past it in the cleanup job, with the planned test. Confidence: high.

### M4. The 8 KB Caddy limit does not cover the password forms

- Spec: plan 32 "Caddy's request size and timeout limits ... move with them"; plan 222 "keeps every form within Caddy's 8 KB request limit"; `spec/security_audit_2026_11.md:161` records the same.
- Code: `deploy/compose/Caddyfile:14-23` limits `/api/v3/auth/hub/*` only; `/oauth/password`, `/oauth/password-link` and `/oauth/register` go through `handle_path /api/v3/*` with a 10 MiB cap and the API's own `BodySizeLimit` (`app/api/body_limit.py`). `Form(max_length=1024)` validators run after Starlette has read the body.
- What a user sees: nothing in normal use; an attacker can hold a worker with multi-megabyte form posts to an unauthenticated route that then does Argon2 work only up to `hash_concurrency`, so the cost is bandwidth and parsing rather than memory.
- Fix: add `/api/v3/oauth/*` to the Caddy block with the same `request_body max_size 8KB` and 20 s timeout, and mention it in `deploy/compose/README.md:330-333` for other proxies. Confidence: high that the limit is missing; low severity.

## 3. Missing tests (against the "Tests that must exist" lines and the task lists)

Covered lines are listed in the table; these are the gaps.

- Password: "lock leaves other methods working" has no test (no test signs a locked password account in through a provider). `tests/test_auth_password_sign_in.py:119` only shows the failure page for the password path.
- Password: "only a new link clears a lock" is covered indirectly (`test_a_reset_clears_the_lock...`, `test_unknown_browsers_locked_at_90_and_correct_password_refused_then`); no test asserts that a correct password from a known browser does not clear a `locked_at` lock. The code refuses it (`throttle.py:304-305`).
- Links (impl 557): `test_use_refuses_handoff_codes_issued_before` and `test_disabled_login_never_reaches_account_creation` exist in substance (`test_a_reset_clears_the_lock_and_ends_sessions_codes_and_device_approvals`) or are moot (links create no accounts). Fine.
- Credential table (impl 560): "wrong current password counts on the account counter" is covered at `tests/test_auth_credential_routes.py:124`. "own key plus correct current password can change" is superseded by the decision that keys are refused outright (plan 240, decision recorded there). All other rows have a test.
- Registration: `test_pending_sees_waiting_only_with_correct_password` is covered by `tests/test_auth_password_sign_in.py:257`.
- Task 11: `test_spoofed_forwarded_header_does_not_change_address` exists but tests uvicorn's middleware in isolation; nothing checks the deployed chain (Caddy overwriting the header, API not reachable directly). The CI Compose smoke test (`.github/workflows/tests.yml:251-265`) could post a spoofed `X-Forwarded-For` through Caddy and assert the throttle keys on the real address.
- Task 12: `test_trusted_proxy_hops_below_zero_refuses_startup` and `test_rename_needs_recent_sign_in_with_password` do not exist; both are superseded (no such setting; rename rule dropped at impl 357).
- Events: `test_auth_event_never_stores_secrets_by_signature` exists (`tests/test_auth_foundation.py`), but no test asserts a `sign_in_failed` or `locked` row after a password failure, which is why M2 went unnoticed.
- Cleanup: `test_cleanup_purges_auth_event_addresses_and_failures_after_90_days` is missing (M3).
- Settings: `tests/test_auth_passwords.py:102` covers the empty pepper; link, cookie and retention lifetimes are covered (`test_link_lifetimes_out_of_range_are_refused`, `test_zero_or_negative_cookie_lifetime_fails_settings_check`, `test_zero_or_negative_retention_is_refused`).
- Unlink with a password: `test_unlink_allowed_with_password_or_other_identity` is missing (M1).

## 4. Unclear or contradicted spec items

- U1. Refusal codes. Impl 552 names `api_key_not_allowed` and `admin_target_not_allowed`; the code and tests use `hub_session_required` (both cases) and `browser_sign_in_required`, which Task 6b introduced first (impl 357). The CHANGELOG does not name the codes. Record which names clients should rely on; nano-crucible's "stable `code`" need (impl 85) is met either way as long as they do not change later.
- U2. Route prefix. Plan 251, 292 and impl 550 still say `/auth/hub/password`, `/auth/hub/password-link`, `/auth/hub/register`; the built routes are `/oauth/password`, `/oauth/password-link`, `/oauth/register` per the sign-in clients design and CHANGELOG. The spec files should be amended so the next reader does not report this as a mismatch.
- U3. Trusted address. Impl 495 specifies `CRUCIBLE_AUTH_TRUSTED_PROXY_HOPS` (default 0, bundle 1); the build relies on `FORWARDED_ALLOW_IPS` (uvicorn) with `"*"` in the bundle and documents the rule at `docs/hub_auth_operations.md:24` and `deploy/compose/README.md:330`. This is the native mechanism and defensible, but it is only safe while nothing but Caddy can reach `api:8000`; the spec should record the choice and the condition, and the smoke test could check it (see missing tests).
- U4. Username rename rules. Plan 122 and 246 require a recent sign-in and refuse API keys for renaming a password account; impl 357 records the user's decision to leave renames unrestricted and "revisit in Task 13 only if the user asks". Task 13 did not revisit it. Worth an explicit note in the plan's Decisions, since the username is now the sign-in name and the restricted-name check (`assert_self_chosen_username_allowed`) is skipped for administrators renaming themselves.
- U5. Pepper documentation. Plan 218 says "the install guide says so"; today only `docs/hub_auth_operations.md:22` does, and Task 15 owns the guide. Fine if Task 15 keeps it; flagged so it is not lost.
- U6. 501 for pages versus routes. The later decision (impl 258) applies 501 `feature_disabled` to JSON routes; HTML pages answer 404 when passwords or registration are off. Consistent with "the strictest code is 404" but not stated anywhere; a one-line note would settle it.
- U7. Plan 224 says `auth revoke-passwords` forces a change "for everyone after a breach"; the built command deletes the credential rows and leaves people with another login signed out but able to sign in with that login, while the rest need an administrator's link. That matches plan 263-264 (no temporary password, no self-service reset without email) but the plan's wording could be read as "everyone must choose a new password". The CLI's description says what it does; the plan should match.
- U8. Minor code observations, not spec mismatches: `verify_password` returns immediately on a malformed stored hash (`passwords.py:105-108`) without the dummy work the other failures do; a malformed hash cannot occur through the API, so timing is unaffected in practice. `revoke_all_passwords` (branch) revokes `HubTokenFamily` rows directly rather than through `revoke_all_hub_tokens`; the effect is the same today.

## 5. NIST SP 800-63B-4 and OWASP claims checked

- NIST 3.1.1.2 minimum 15 for single factor, maximum at least 64, Unicode accepted, NFC SHOULD, no composition rules, no periodic change, blocklist with reason and guidance, no hints: all met (items 10 to 16). The 12 floor is the user's recorded trade-off (decision 22).
- NIST 3.2.2 rate limiting at or below 100 consecutive failures: met (`TOTAL_LIMIT = 100`, item 21). NIST's requirement to notify or offer a path after a lock: an administrator's link (item 52), no email, recorded as a gap in decision 21.
- OWASP Password Storage: Argon2id with m=19 MiB, t=2, p=1 as the floor, pepper via HMAC pre-hash and base64, rehash on upgrade: met (items 1 to 5). OWASP's "pre-hash then bcrypt" caveat does not apply to Argon2id.
- OWASP device cookies: signed cookie per successful sign-in, failures counted per account instead of per cookie as the plan explains: met (item 23).
- OWASP Forgot Password: single-use token, hashed at rest, no automatic sign-in, generic answers: met (items 45, 49, 50, 52). The audit's note that 24 h (up to 168 h) is long for a reset token stands; it is a recorded setting, not a mismatch.
- OWASP Authentication: generic failure message with equal work (item 32); CSRF on every form (items 36, 49, 67); API keys cannot change credentials (items 28, 61).
