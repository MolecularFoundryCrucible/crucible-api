# Self-hosting Phase 3: production upgrade runbook

Status: draft, 2026-11-06. Covers the work built up to pull requests #446 to #448. Later Phase 3 tasks (sign-in for any client, passwords) add steps here before the release. Production stays on 3.3.1 until the user decides, and every write to production below is run by the user, or with the user's approval at that step.

## What changes

Production runs 3.3.1, schema `564d0bd33a5c`. The release brings 11 Alembic revisions (`migrations/versions/20261103_*` to `20261106_*`), all additive: new tables (`user_identity`, `auth_event`, `api_key`), new nullable columns, and temporary triggers that keep old pods' writes correct during the rollout. It also brings three data scripts, two settings, and one switch.

Old (3.3.1) pods keep working against the new schema: every new column is nullable or has a default, and the readiness check treats a schema ahead of the build as ready.

## Order

### Before the release, on 3.3.1

1. **Dry runs, read only.** In a production pod: `20261105_fill_owner_user_id.py --unmatched unmatched_owners.csv` and `20261106_backfill_api_keys.py`, without `--commit`. Record the counts. Expected from the 2026-11-05 counts: 6,952 datasets and 254 samples to fill, and 834 datasets plus 66 samples whose owner names nobody.
2. **Fill dataset and sample owners, with `--commit`, still on 3.3.1.** 3.3.1 already writes `owner_user_id` for new records, and the script only fills empty ones, so this is safe on the running release. It must come before the new code: the new code reads owners only from `owner_user_id`, and any update before the fill would write a stored record version with no owner. Keep the CSV of unmatched owners for the user to assign by hand. Check: no dataset or sample left with an empty `owner_user_id` whose `owner_orcid` names a user.

### The release

3. **Migrate.** `python -m app.cli db upgrade --yes-this-is-production`. Each migration commits on its own and waits at most 5 seconds for a table lock (`ALEMBIC_LOCK_TIMEOUT`). If one times out behind a long transaction, nothing is half done: run the command again. 3.3.1 pods keep serving throughout.
4. **Settings, through the deployment repository.** `CRUCIBLE_AUTH_PROVIDERS` and `OIDC_ORCID_CLIENT_SECRET_VERSION` in both `foundry/main/deployment.yaml` and its ConfigMap, or the new image's settings check fails. `EXPECTED_DB_REVISIONS` set to the new head. `CRUCIBLE_AUTH_IDENTITY_LOOKUP` stays off. Remove `HUB_AUTH_ORCID_*` only after the new image is verified.
5. **Deploy the new image** (the deployment repository's approved pull request). Old and new pods run side by side. The triggers cover what old pods write: people recorded by internal number, and a key deleted by an old pod is revoked in the hashed table.
6. **After the last 3.3.1 pod is gone,** dry run then `--commit`: `20261103_backfill_user_identity.py --issuer https://orcid.org`, then `20261106_backfill_api_keys.py`. Both only insert (the key backfill also names unnamed keys) and are safe to rerun. Check: every person with an ORCID identifier has an identity row, and every owned `apitoken` row has a hashed copy.
7. **Turn on `CRUCIBLE_AUTH_IDENTITY_LOOKUP`** and restart. Sign-in now reads the identity table, and linking becomes available.
8. **Checks:** `/health/ready` on every pod; a real Hub sign-in; an existing API key, an older key without the prefix, and a newly created named key all work; crucible-tests against production through the deployment session, without creating users.

## Rolling back

| Stage reached | Safe rollback | Notes |
|---|---|---|
| Steps 1 and 2 | nothing to roll back | only values 3.3.1 itself writes were added |
| Step 3, migrated, still on 3.3.1 | stay on 3.3.1 | the schema is additive; a downgrade is possible but not needed |
| Steps 5 and 6, new image running | back to the 3.3.1 image, until the point of no return | revocations by 3.3.1 reach the hashed table through the trigger |
| Past the point of no return | forward fixes only, or images with the new code | see below |

The point of no return is the first of these, because 3.3.1 does not know about them:

- a person is disabled (`disabled_at`): on 3.3.1 they could sign in again;
- an account is waiting for approval (`pending_at`): on 3.3.1 it is an ordinary account;
- a named key is created under `/account/apikeys`: it exists only in the hashed table and stops working on 3.3.1;
- a login is linked, after step 7: 3.3.1 signs in by ORCID only.

Until then, do not disable anyone, switch a provider to `approval`, or announce named keys. Rolling back after step 7 also needs the switch turned off first.

## Clients

| Client | Against 3.3.1 | Against the release |
|---|---|---|
| nano-crucible 3.3.0 (released) | works | works: API keys, `api_key_status` and `/account/verify` keep their shapes; regenerate is deprecated but works |
| nano-crucible, next release | works if it checks for the new routes and falls back | uses named keys |
| Hub, current | works | works: `/auth/hub` routes unchanged |
| Hub, with the key list | must not be deployed before the API release | uses `/account/apikeys` |

## Before this runbook is used

- The user approves each production step.
- Rehearse on a copy of production data, as planned for step E, timing steps 2, 3 and 6.
- The deployment session confirms the settings in step 4 against the real files.
- Update this document with every later Phase 3 task that adds a migration, script or setting.
