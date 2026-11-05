# Crucible Phase 3 security review

Date: 2026-11-05. Code reviewed: crucible-api `main` at `1d32764`, the Phase 3 plan at docs commit `3637eb1`, crucible-hub `origin/main`, nano-crucible `origin/main`.

## Bottom line

- **Nothing is being exploited that I can see, and nothing critical stands.** Agents labelled 4 findings critical. I reviewed those claims and rate the issues behind them high at most.
- **One real data exposure bug is live in production code, with no records affected today.** Editing a dataset can silently make it public again. I reproduced it locally. It should be fixed first, and it is small.
- **The biggest structural risk is a leaked administrator API key.** It can take over other administrators today. Your Task 6b already covers this, so I recommend moving it up. The review also found gaps in Task 6b itself.
- **The new OIDC code held up well.** The sign-in protocol checks (state, nonce, PKCE, issuer, audience, signatures, per-provider callbacks) drew no finding that survived checking at a serious level. The one real fix there is requiring `https` on discovery addresses.
- **The password and ID move plans need changes before they are built**, listed in sections 2 and 3. These are cheap to change now.
- **How many agents agree is a poor guide to what is real.** The worst bug was found by 1 of 30 reviews. A claim made by 8 reviews was false. Every verdict below comes from my own check, not from the vote.

## Severity and how to read this

- **Agents** counts reviews, out of 30, raising the issue. It comes from keyword matching, so treat it as approximate, and an issue outside a batch's starting scope had less chance of being raised.
- **Checked** says how I confirmed it: **Reproduced** (ran it), **Code** (read the code), **Plan** (read the plan text).
- Severity is my judgment, and often differs from the agents' label.
- Line numbers are at `1d32764`.

## 1. Real issues in built code

| # | Issue | Severity | Agents | Checked | Fix size |
|---|---|---|---|---|---|
| 1.1 | Editing a dataset can re-publish or un-publish it | **High** | 1 | Reproduced | Small |
| 1.2 | An admin API key can take over other administrators | **High** | 18 | Code | Small to medium |
| 1.3 | Legacy upload lets a contributor overwrite or delete others' files | **High on GCS** | 5 | Code | Medium |
| 1.4 | Editors can publish through `PATCH` | Medium | 3 | Code | Small |
| 1.5 | Anyone can make anyone else a project owner | Medium | 10 | Code | Small |
| 1.6 | Any signed-in person can look up people by email | Medium | 7 | Code | Your decision |
| 1.7 | nano-crucible sends the API key to storage hosts | Medium | 4 | Code | Small, in nano-crucible |
| 1.8 | The API key page loads a third-party script | Medium | 12 | Code | Small |
| 1.9 | Discovery addresses are not required to be `https` | Medium | 6 | Code | Small |
| 1.10 | `/keywords` is public and includes private datasets' keywords | Low to medium | 6 | Code | Small |
| 1.11 | Several smaller items | Low | see below | Code | Small |

### 1.1 Editing a dataset can re-publish or un-publish it

- **Where:** `app/api/routes/datasets.py:364-368`, with `app/api/routes/resources.py:943,963`.
- **What happens:** `PATCH /datasets/{id}` always copies the old `Dataset.public` column onto the real public grant. `PUT` and `DELETE /resources/{id}/access/public` change only the grant and never update that column.
- **Reproduced locally:**
  - Created public, unpublished, then renamed: **public again**, with the public ACL row restored.
  - Created private, published through the access route, then renamed: **private again**.
- **Who is affected:** anyone who unpublishes a dataset that was created public, then edits it, or has it edited, including by automation. It happens with no warning.
- **Production today:** `v3.3.1` has this code. Read-only count: 17 public datasets, **0 in the state where an edit would re-publish them, 0 where it would un-publish**. Staging has 1 that would un-publish. So it has not caused harm so far that this count can show, though it cannot rule out a dataset that was unpublished, re-published by an edit, and now looks consistent.
- **Samples:** not affected; they change the grant only when `public` is in the request (`samples.py:186-188`).
- **Fix:** do what samples do, and touch the grant only when `public` is actually in the request. Add both sequences above as tests.

### 1.2 An admin API key can take over other administrators

- **Where:** `app/api/routes/users.py` (`PATCH /users/{ref}/platform_role`, `POST /users/{ref}/apikey`, `/apikey/regenerate`), and `require_recent_authentication` in `auth_utils.py`, which returns immediately for API keys.
- **What happens:** with a leaked admin key, someone can demote or promote administrators and read or regenerate another administrator's key. This also gets around the protection I added to disabling in #423: demote first, then disable. On `platform_role`, a stale Hub session is not asked to sign in again either.
- **Status:** you already decided this in Task 6b, but it isn't built. Admin keys last a year.
- **Fix:** build Task 6b now rather than later, with the gaps in 2.1 closed.

### 1.3 Legacy upload lets a contributor overwrite or delete others' files

- **Where:** `app/api/routes/gcs_storage.py`, `_mint_upload_token` and `_validate_and_prepare_upload`.
- **What happens:** the route needs only `Permission.CREATE`, but the token it returns is `roles/storage.objectAdmin` over the whole `{dataset}/` prefix. Any contributor on a dataset, including a service account, can overwrite or delete every existing file there directly in storage, and the database record won't reflect it. Replacing a file through the newer multipart route correctly needs `EDIT`.
- **Limits:** Google storage only. On production, bucket versioning keeps deleted versions for 30 days (from `docs/file_replace_design.md`), so this is recoverable if noticed.
- **Fix:** narrow the token to the single new object and to create-only, or retire the route once clients have moved. I'd check which client versions still use it first.

### 1.4 Editors can publish through `PATCH`

- **Where:** `datasets.py` `update_dataset` needs `EDIT` (admin route `PUT .../access/public` needs `MANAGE_ACL`). `public` is an accepted field.
- **Samples:** same gap. `update_sample` also needs only `EDIT` (`samples.py:176`).
- **Fix:** require `MANAGE_ACL` when a request changes `public`, for both, in the same PR as 1.1.

### 1.5 Anyone can make anyone else a project owner

- **Where:** `app/api/routes/projects.py:77-130`.
- **What's deliberate:** letting anyone create a project is intended (`can_create_project=True`). Agents calling that a missing permission are wrong.
- **What's real:** a non-administrator can name any user as lead by ORCID, username or email, making them owner without consent, even though the API reports `can_create_for_others` as false. The response also returns the lead's ID.
- **Fix:** non-administrators can name only themselves.

### 1.6 Any signed-in person can look up people by email

- **Where:** `GET /users?email=` skips the shared-group filter (`users.py`, `exact_lookup`), `POST /users/resolve`, `GET /users/{email}`.
- **What happens:** any account can map an email to a name, username and ID, across projects and in bulk. ORCID registration is open, so in practice almost anyone can do this.
- **Decision needed:** adding people by email is a real feature. Options: administrators and project admins only, or exact single lookups with a rate limit. I'd pick the second.

### 1.7 nano-crucible sends the API key to storage hosts

- **Where:** `nano-crucible/crucible/resources/gcs/download.py:97` and `files.py:111` use the authenticated session for signed URLs. Its uploads correctly use plain `requests`.
- **What happens:** at the Foundry, the API key goes to Google with each download. The Compose bundle's Caddy strips it before the store (`Caddyfile`), so bundle deployments are protected unless they use a separate S3 host.
- **Fix:** use a plain request for signed URLs. This is for the nano-crucible session.

### 1.8 The API key page loads a third-party script

- **Where:** `app/api/routes/auth.py:124,175`, the email prompt from `/auth/apikey`.
- **What happens:** the page holds a live, year-long API key and loads Bootstrap's JavaScript and CSS from jsDelivr, with no integrity check and no CSP. A compromised CDN could read the key.
- **Fix:** remove the script or self-host it, and add SRI and a CSP.

### 1.9 Discovery addresses are not required to be `https`

- **Where:** `app/auth/providers.py:67-82`.
- **What happens:** `token_endpoint`, `jwks_uri` and `authorization_endpoint` must only be present. A bad or compromised provider document could send the client secret to an internal or plain-HTTP address. Agents rated this high, but only a provider an administrator listed can do it, and that provider can already vouch for its own users, so I rate it medium.
- **Standard:** OIDC Discovery requires these to be `https`.
- **Fix:** require `https`, with `http` only when the issuer itself is `localhost`.

### 1.10 `/keywords` is public

- **Where:** `app/api/routes/misc.py:134-159`, no authentication.
- **What happens:** anyone can list keywords and counts from all datasets, private ones included. It doesn't reveal which dataset, but keywords can be revealing.
- **Fix:** require sign-in, after checking that the Hub and clients don't call it anonymously.

### 1.11 Smaller items

| Item | Where | Fix |
|---|---|---|
| Disable and enable skip recent sign-in for ordinary users in a Hub session; the plan says it's required. **My miss in #423** | `users.py` `_account_to_switch` | Always call `require_recent_authentication` |
| `POST /users` doesn't record the administrator. **My miss in Task 5** | `users.py`, `provision_user` | Pass `created_by` |
| Refused sign-ins of known people aren't in their history | `sign_in.py` `record_refused_sign_in` | Pass the user when known |
| Any site can cancel someone's sign-in by sending them to the callback, as the login cookie is deleted even on no match. Same on the Hub | `hub_auth.py:196`, `hub-auth.server.ts:514` | Delete only when matched |
| Username can be cleared to null, which breaks once it's `NOT NULL` | `models/users.py` | Refuse null |
| Demoting an admin leaves their key cached for up to 60 seconds | `users.py` | Clear this worker's cache |
| Same-site POST to `/auth/apikey/regenerate` has no CSRF check; cross-site is blocked by `SameSite=Lax`, see section 5 | `auth.py` | `Origin` check |
| Release tag is put directly into a shell command, with `contents: write`. Only people with release rights can set it | `.github/workflows/openapi.yml` | Pass through `env` |
| `first_thumbnails` and `users/resolve` have no batch limit | `datasets.py`, `users.py` | Cap the batch |
| Filenames aren't validated on the record route for GCS | `models/datasets.py:291-297` | Validate as uploads do |
| `is_orcid` is loose, accepting any capitals | `general_utils.py:9` | Use the strict pattern |

### Hub items, for the Hub session

- **Step-up omits `required_user`, and the callback ignores `error`** (15 reviews). Expected, because the Hub hasn't adopted the contract from #419 yet, but it is why this matters: until then, signing in as someone else during step-up can create a stray account.
- **Login can lock up in single-instance mode** (4 reviews). `enforceHubLoginRateLimit` checks the pending count before expired entries are pruned, so 500 abandoned logins refuse everyone from then on. The Compose bundle uses Postgres mode, so it affects local and single-instance setups.
- **Step-up may reset elevated mode to normal** (2 reviews). It fails safe. I didn't test it.

### Also open from earlier

Replaced files aren't retired on staging (`403 ... Provided scope(s) are not authorized`). You chose to leave this for later, and it's in the plan.

## 2. Plan changes before building (Tasks 6 to 13)

All from reading the plan text. None of this is built yet.

| # | Issue | Agents | Change |
|---|---|---|---|
| 2.1 | **Task 6b has gaps.** Rules say "another human administrator", so a leaked admin key can issue a reset link for **its own** admin account, set a password, and become a full session. `PATCH /users/{ref}` (username is the sign-in name, plus email) and `DELETE /users/{user}/password` aren't covered | 8 | Refuse for any human administrator target, including self, and add both routes |
| 2.2 | **A leaked API key can disable its owner's password.** "Key plus the current password, on the same attempt counter", limit 10 with no delay, then disabled until an administrator acts. Ten requests and it's locked. This contradicts your rule that a key can't change how someone signs in | 4 | Refuse `POST /account/password` with an API key; the CLI prints a browser link instead |
| 2.3 | **Device cookie contradiction.** The plan says it carries `password_changed_at`, but the implementation signature `read_device_cookie(value, user_id)` can't check that | 6 | Fix the signature |
| 2.4 | **"Ends other Hub sessions" can't work yet.** `Principal` has no token family | 3 | Add the family ID to `Principal` |
| 2.5 | **One login cookie.** Parallel tabs or a popup overwrite each other, so the plan's claim that they "keep working" is false. It also affects linking and step-up | 3 | One cookie per sign-in, named from the transaction |
| 2.6 | **The link confirm page can't name the login.** The Hub gets the identity only on confirm | 3 | Add a preview step |
| 2.7 | **Pending accounts can be looked up and added to projects**, after which rejecting returns 409 forever | 3 | Hide pending accounts from lookups in Task 7 |
| 2.8 | "Verify your ORCID" needs the link step's "identity is free" check to allow your own row | 1 | Say so in Task 6 |
| 2.9 | Counting before hashing must still allow the 10th attempt | 3 | Pin it with a test |
| 2.10 | `POST /account/password` is outside `/auth/hub`, and a 422 echoes the input | 1 | Hide inputs on password routes |
| 2.11 | `form-action 'self'` may block the redirect after the password POST; the Hub session raised this too | 2 | Test in a real browser |
| 2.12 | `resolve_sign_in` takes an issuer, so Task 7 will need to pass the registration rule | 1 | Add the argument then |

**One residual to accept or change:** someone with a stolen device cookie or on a shared computer can disable a password in 10 guesses, since the known-browser counter has no delay. That was a deliberate trade-off. I'd consider a small delay after a few failures, which doesn't reopen the remote lockout attack.

## 3. The ID move (Task 5a)

No maintenance window still looks workable. These are changes to the plan, and I'd fold them in before starting.

| # | Issue | Agents | Change |
|---|---|---|---|
| 3.1 | **Stale identity during a person's rewrite.** For up to 60 seconds, cached sign-ins hold the old ID, so requests fail and a replacement upload can be rejected. Agents said this "destroys" uploads: the new upload is rejected and needs retrying, but the original file is intact | 12 | Finish moving permission checks to `user.id` first. Scale: 87 lines compare the caller's ID, and 184 compare `.unique_id` |
| 3.2 | **My audit log exception is wrong.** Keeping `deletionauditlog` as text means people lose "my deletion history", because `deletion.py:918-925` compares it to their current ID | 3 | Add internal ID columns, and keep the text as history |
| 3.3 | **Trigger list is incomplete.** Join requests and postprocessing runs are missing. They're empty on production but not on staging, and old pods can write them | 5 | Add triggers for every column that moves |
| 3.4 | **Direct readers of `owner_orcid`** beyond routes: `gcs_record_utils.py`, metadata search, facets | 7 | Add a checklist; drop the column only when a search finds none |
| 3.5 | **The one-time Hub session clear can't work across batches.** People rewritten later would still hold sessions created after it | 2 | Revoke each person's tokens in the same transaction as their rewrite. This may remove the need for a clear; confirm with the Hub session |
| 3.6 | **Unlinked ORCID after the rewrite.** The check that refuses it looks at `unique_id`, so it can never fire. With ORCID `open`, an unlinked ORCID would create a new account | 2 | Your decision: keep a record of unlinked ORCIDs and refuse, or accept a new account |
| 3.7 | **One production person has an odd ID** ending in a lowercase letter (`9999-9999-9999-999a`), so the backfill skips them and real ORCID sign-in doesn't match them | 2 | Look at that record by hand before the backfill |
| 3.8 | **`ApiToken.access_group_id` holds an ID too**, and 5 production keys already differ from their owner | 1 | Add to the rewrite, and check those 5 |
| 3.9 | **Instruments with no owner** break the response model | 1 | Already known; make the field optional |

**Production owner data, for reference.** Where more than one record of an owner exists, they never disagree. See the plan for the full counts. About 180,000 ownerless datasets are already documented in `docs/sharing_collections_followup.md`.

## 4. Decisions only you can make

1. **Service accounts can read everything** (3 reviews). Every service account is a global contributor (`service_accounts.py:171-178`, `permissions.py:247-253`), so it can read all private records and create anywhere. The permission design says this is deliberate, for ingestion. A leaked ingestion key therefore reads all data. I'd plan to scope them to projects and instruments, but not in Phase 3.
2. **Email lookup** (1.6): I recommend exact lookups only, with a rate limit.
3. **Instrument metadata** (4 reviews): `scientific_metadata` goes to any signed-in person (`resource_details.py:300-302`). Are instruments meant to be open? If yes, this is fine.
4. **Deletion requests** (1 review): a contributor can freeze and hide a dataset until someone reviews the request. It's documented, but it allows griefing.
5. **Unlinked ORCID after the rewrite** (3.6).
6. **Task 6b's order:** I recommend building it next.

## 5. Claims I rejected

These show why the counts can't be trusted alone.

| Claim | Agents | Why it's wrong |
|---|---|---|
| Disabled administrators count toward the last-admin check, allowing total lockout. Rated up to high | 8 | The caller must be an active human administrator and can't target themselves, so one always remains. The only gap is a caller disabled within the 60-second cache |
| Legacy cookies have no `SameSite`, so cross-site CSRF works. **I believed this at first too** | 11 | Starlette defaults to `SameSite=lax`, which I confirmed by running it. Only same-site requests remain, which is why it's low in 1.11 |
| `max_age` isn't verified, so step-up can be bypassed | 7 | Recent sign-in is decided from the token's `auth_time`, and a missing value counts as not recent. A provider ignoring `max_age` causes a failed step-up, not a false pass |
| Missing signing algorithms in discovery allow a bypass | 6 | Authlib falls back to RS256 only. An explicit refusal would be tidier, but nothing is exploitable |
| `X-Forwarded-For` can be spoofed through the bundle | 8 | Caddy's docs say it ignores incoming values unless `trusted_proxies` is set. Only the proxy and Hub reach the API. The Foundry's front door still needs its own check at Task 11 |
| Stored XSS through `/crucible-files` | 1 | The quoted Caddy block doesn't exist. The real one strips credentials and sets `CSP: sandbox` and `nosniff` |
| First sign-in race refuses valid people | 2 | Postgres tests with 8 simultaneous sign-ins pass |
| Anyone creating a project is a missing check | Several | It's deliberate; only the part in 1.5 is real |
| Audit address trusts `X-Forwarded-For` in `events.py`, and a `crucible login` loopback task | 1 each | Neither exists. One agent invented the code it quoted |

## How the review ran

- **Agents:** 30 reviews by `gemini-4-argon-high`, 6 per batch, at most 6 at once, with no rate-limit failures. One run was interrupted and retried.
- **Batches:** (1) sign-in protocol, (2) accounts and authorization, (3) the planned design, (4) the ID move and deployment, and (5) authorization across the rest of the API. **Batch 5 is an addition to the 4 you asked for**, because agents kept finding real issues outside sign-in.
- **Output:** 293 findings. Agents rated 4 critical, 101 high, 156 medium, 32 low.
- **Reliability:** I checked every quoted line against the code automatically. **70% matched exactly** (1074 of 1545). It ranged from 58% in batch 1 to 90% in batch 3, after I told agents quotes would be checked. Not every miss is invented, since many are reformatted, but some were invented outright.
- **Isolation:** each agent ran in a `bwrap` sandbox on a read-only copy of the code, with my home directory hidden. I tested this with a canary file. They had only `agy`'s login token and could reach the internet. They could not see your kube, gcloud or SSH credentials, or your `.env` files.
- **Sent to Google:** the API, Hub and nano-crucible source, and the plan docs, which include the production totals. No people's data, and no secrets I could find by searching for key patterns.
- **My own access:** read-only queries on staging and production, aggregates only. No code changed, nothing pushed, and staging and production were not touched. The one thing I ran was a throwaway local test for 1.1, which I deleted.

## What this does not cover

- **No live testing.** Apart from 1.1 and a few small checks, these are verdicts from reading code. Nobody attacked a running server.
- **Not every finding got equal depth.** I checked everything serious and spot-checked the rest. Lower-severity items may include some wrong ones.
- **Possibly missed issues.** 1.1 was found by a single review, so there may be others no agent found.
- **Not reviewed:** the ingestion consumer, the Foundry's Kubernetes and front door, and dependencies. The Hub and nano-crucible got only what agents followed from the API.
- **Not a replacement for Task 14**, the independent security review at the end of the plan.

## What I'd do next

1. **Fix 1.1 and 1.4 together.** It's a real exposure bug, small, and worth releasing without waiting for Phase 3, if you agree. Production can take that as an ordinary patch.
2. **Build Task 6b now**, with 2.1 and 2.2 folded in, plus the two items I missed in 1.11.
3. **One hardening PR:** 1.5, 1.8, 1.9, 1.10 and the rest of 1.11.
4. **Look at 1.3** once we know which clients still use the legacy upload.
5. **Send the client items** to the nano-crucible and Hub sessions: 1.7 and the Hub list.
6. **Correct the plan** with sections 2 and 3, then start Task 5a.

Raw agent output is kept outside the repo, in `crucible-api-reviews/raw/` on the reviewer's machine.
