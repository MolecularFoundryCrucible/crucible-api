# Permission Migration Runbook

Operational companion to `docs/permission_system_plan.md`. That document is the
design — what the model is and why. This one is what to type, in what order,
and what will bite you.

Staging is through phases 0–3. Production has not started. Everything below
reflects what actually happened rather than what was planned, including the
ordering mistakes made along the way.

---

## Production checklist (start here)

Nothing below has run on production yet. Each numbered step is a checkpoint —
finish it before starting the next. `alembic upgrade head` is safe to run any
time (Alembic tracks its own state); every `--commit` script should be run
without the flag first (dry run) and checked before re-running with it.

1. `DELETE FROM idtype WHERE mfid = '' AND resource_type = 'sample';` (the one open precondition blocker)
2. `python migrations/data/20260806_audit_permission_preconditions.py` — confirm clean
3. `python migrations/data/20260806_apply_sql.py migrations/data/20260806_add_permission_roles.sql`
4. **Deploy** (the base permission-role code, with the admin-group bypass)
5. `python migrations/data/20260806_backfill_permission_roles.py --commit`
6. `alembic stamp head`, then `alembic upgrade head` (catches up every later schema change in one go — all additive, safe regardless of code version)
7. `python migrations/data/20260810_provision_instrument_operator_groups.py --commit`
8. `python migrations/data/20260810_backfill_instrument_id.py --commit`
9. `python migrations/data/20260810_backfill_project_unique_id.py --commit`
10. `python migrations/data/20260810_rename_project_groups_to_mfid.py --commit`
11. **Deploy** (Group C)
12. `python migrations/data/20260811_backfill_project_mfid.py --commit`
13. `python migrations/data/20260811_backfill_instrument_mfid.py --commit`
14. `python migrations/data/20260813_repoint_project_resource_identity.py --commit`
15. **Deploy** (project/instrument identity + project's-own-identity + join-request FK + rename support — everything currently on `feat/permission-roles` past Group C)
16. Re-run steps 12–14 (idempotent — catches anything created in the gap between backfill and deploy)

Full context, verify-queries, and why each ordering constraint exists: see the
matching section below (`Procedure`, `Group C rollout`, `Project/instrument
identity rollout`, `Project's own resource identity + join-request FK`,
`Project rename support`).

---

## Schema migrations moved to Alembic (2026-08-10)

Everything below this table predates it. `migrations/` used to be a flat
folder of one-off `.sql` files and `apply_sql.py`; that history is unchanged
in behavior but now lives in `migrations/data/`, renamed `YYYYMMDD_` by
first-commit date so ordering doesn't require `git log`. Data backfills,
audits, and cleanup scripts (the dry-run-by-default, production-guarded
Python scripts) stay in `migrations/data/` going forward too — Alembic is for
schema shape, not risky data mutation.

New schema changes: `DB_CONF_FILE=.env alembic revision --autogenerate -m
"..."`, review the generated file, `DB_CONF_FILE=.env alembic upgrade head`
(staging first), then the same against production with
`-x yes-this-is-production=true`.

**Staging and production are NOT at the same schema state**, which matters
for adopting Alembic cleanly: staging has already run
`migrations/data/20260806_add_permission_roles.sql`; production hasn't
(Phase 1a below is still open there). The baseline revision
(`migrations/versions/20260810_fc9be1ddfb92_baseline_adopt_alembic_schema_as_of_.py`)
is empty — it marks
"schema as of 2026-08-10" and assumes that column already exists. So:

- **Staging**: already at the baseline state. Run `alembic stamp head` now — no DDL, just marks the version table.
- **Production**: run Phase 1a exactly as written below (the old, already-tested `apply_sql.py` path — this migration is mid-rollout and re-authoring it as a fresh Alembic revision would be untested on the one path that matters, the actual rollout). Only after that succeeds does production also get `alembic stamp head`, at which point both databases agree with the baseline and every subsequent schema change is a real Alembic revision.

---

## Status

| Phase | Staging | Production |
|---|---|---|
| 0 — green test suite | done | n/a (repo-wide) |
| 0.5 — precondition audit | done, clean | done, **1 blocker left** |
| 1 — expand schema | done | not started |
| 1 — backfill | done | not started |
| 2 — inert code (`Principal`, `effective_role`) | deployed | not deployed |
| 3 — shadow mode | deployed, flag on | needs prod traffic |
| 4 — cutover | not started | not started |
| 5 — contract (drops) | not started | not started |

**Branch:** `feat/permission-roles`, 48 commits, **not merged to `main`**.
Deployed to staging by pushing `feat/permission-roles:staging` (fast-forward).
Test suite: 757 hermetic, 856 with a throwaway Postgres (99 skipped without one).

This table covers phases 0–5 of the original Role/Permission rollout only.
Everything staging has additionally received since — instrument/project
identity (MFID groups, `project_mfid`/`instrument_mfid`), the project's own
resource identity fix, the join-request FK fix, and project rename support —
is tracked in the dated `## ... rollout (staging)` sections below instead,
and rolled into the production checklist at the top of this document.

### What has been built

| Area | Where |
|---|---|
| Pure permission model — `Role`, `Principal`, `effective_role`, `has_permission`, `can_grant` | `app/utils/permissions.py` |
| Auth returns a `Principal`; admin-group bypass; public-group injection | `app/utils/auth_utils.py` |
| Shadow mode | `app/utils/shadow.py` |
| Precondition audit (read-only) | `migrations/data/20260806_audit_permission_preconditions.py` |
| Additive schema | `migrations/data/20260806_add_permission_roles.sql` |
| Backfill (dry-run default, idempotent, self-verifying) | `migrations/data/20260806_backfill_permission_roles.py` |
| Batched delete of the 721k bypass rows | `migrations/data/20260807_delete_bypass_acl_rows.py` |
| `.sql` runner using the app's own DB config | `migrations/data/20260806_apply_sql.py` |
| Exhaustive role matrix (221 assertions, no DB) | `tests/test_permissions.py` |
| Agreement tests — old checks vs new, on real migrated data | `tests/test_permission_agreement.py` |
| End-to-end migration test against real Postgres | `tests/test_migration_permission_roles.py` |

### Bugs found and fixed along the way

Each of these would have shipped:

| Bug | Consequence if missed |
|---|---|
| `can_grant` ignored the role being replaced | A project admin could demote the lead to viewer |
| Backfill overwrote a project's `owner` grant on itself | Every project lead loses the ability to transfer their project |
| `Principal` carried no public group | Everyone loses read access to all public data at cutover |
| Deleting admin ACL rows before the bypass shipped | Every instrument service account offline, at the step billed as inert |
| `effective_role` took the last of duplicate ACL rows | Silent under-granting on any fan-out join |
| `has_permission` defaulted to the weakest check | A forgotten argument fails open |
| `project_id` unvalidated | **Live escalation**: `POST /projects {"project_id":"admin"}` makes any user a platform admin |
| Uploaders sending `project_id=""` | 1,292 datasets filed under a nameless project; still arriving |

### Data cleanups (production)

| What | Status |
|---|---|
| 567 samples naming a nonexistent project | deleted, `~/crucible-audits/dangling-sample-projects.md` |
| 1,292 datasets under the empty-string project | documented, not actioned, `~/crucible-audits/empty-project-datasets/` |
| 1 orphaned `idtype` row blocking the backfill | **open** |

### Decisions worth not relitigating

- Service accounts **are** promoted to `platform_role='admin'`, temporarily, so cutover does not stop ingestion. Narrowing them is tracked in the plan.
- No human holds `platform_role='admin'`. There is currently no human break-glass path.
- Existing project members backfill to `editor`, not `contributor`, so nobody loses access on day one.
- `read_only` has one member, a developer, who loses access. Accepted.
- Facility staff lose instrument-derived access to private datasets; they keep public ones via the public group.
- Group names should be MFIDs, not `project_id`/`instrument_name`. Deferred to phase 4.

---

## Two ordering rules, both learned by breaking them

**1. The admin-group bypass ships before the ACL rows are deleted.**

`create_standard_acls` writes an `admin` ACL row on every resource — 360,726 on
production. Admin-group members reach everything through those rows, and on
production **all 8 admin-group members are service accounts whose only other
group is their own ORCID group**. `platform_role` does not become load-bearing
until cutover, so deleting those rows while the old checks are running takes
every instrument offline.

`auth_utils._is_admin_by_group` closes this: the pre-role helpers honour
admin-group membership directly. It must be **deployed** before the backfill
runs. On staging I ran the backfill first and briefly broke exactly this.

**2. The schema migration runs before the code that reads the new columns.**

Phase 2 adds `role` and `platform_role` to the SQLModel models, so the deployed
code selects those columns on every user lookup. Deploy that against a database
without them and the API 500s on essentially every authenticated request.

Net ordering per environment: **migrate schema → deploy code (with bypass) →
run backfill.** Not the order the plan's numbered list implies.

**3. The project-group MFID rename has no admin-bypass equivalent — it is a
hard cutover, not a staged one.**

Group C's project half (`app/utils/project_utils.py` and friends resolving a
project's access group via `Project.unique_id` instead of `project_id`) is
written but not deployable yet. Unlike rule 1, there is no fallback path: the
code looks a group up by MFID, and if `migrations/data/
20260810_rename_project_groups_to_mfid.py` hasn't renamed it yet, every
project route 403s or 404s, project-wide, the moment the code deploys.
Required order: **`Project.unique_id` backfill → group-rename backfill →
deploy the code.** See the two scripts' own docstrings for the same warning.

---

## Production prerequisites

Both surfaced from the audit. Staging showed neither — it is **not** a clone of
production (project list plus ~1/120th of the data), so preconditions must be
measured against production directly.

| Blocker | Status |
|---|---|
| 567 samples naming a nonexistent project | **resolved** 2026-08-07, see `~/crucible-audits/dangling-sample-projects.md` |
| 1 `project_id` colliding with a resource of another type | **open** |

The open one is a single orphaned `idtype` row with `mfid = ''` claiming to be
a sample, with no sample behind it and no ACL rows. It collides with the
empty-string project's `project_id`, which makes the backfill refuse to run:

```sql
DELETE FROM idtype WHERE mfid = '' AND resource_type = 'sample';
```

The empty-string project itself holds 1,292 datasets and does **not** need
resolving first — see `~/crucible-audits/empty-project-datasets/`.

### Scale, which staging cannot show

| | staging | production |
|---|---|---|
| `resourceacl` rows | 47,614 | 1,166,523 |
| rows the backfill deletes | 22,443 | **721,231** (61%) |
| datasets | 2,880 | 348,107 |

721k deletes in one transaction is the one step whose runtime and lock profile
are untested. Consider batching, or a maintenance window. Staging finished
instantly and proves nothing here.

---

## Procedure

Every script prints the database it connected to and refuses production without
`--yes-this-is-production`. Production and staging are the **same Cloud SQL
instance**, differing only by database name, so that guard is the only thing
between the two.

### Setup

```bash
cloud-sql-proxy --port 5433 mf-crucible:us-central1:crucible-db
```

`.env` must have `DB_DATABASE=crucible-staging` (or `crucible`). Note that
`.env.example-localdev` has `DB_DATABASE=crucible` — do not copy it blindly.
Always pass `DB_CONF_FILE=.env` explicitly rather than relying on the cwd
fallback.

### Phase 0.5 — audit

```bash
DB_CONF_FILE=.env python migrations/data/20260806_audit_permission_preconditions.py
```

Read-only. Four counts: public-column drift, dangling `Sample.project_id`,
`read`/`write` distribution, multi-member ORCID groups. Resolve anything
non-zero before continuing.

### Phase 1a — schema

```bash
DB_CONF_FILE=.env python migrations/data/20260806_apply_sql.py migrations/data/20260806_add_permission_roles.sql
```

Additive only. Verify the columns are the enum types, not `varchar`:

```sql
SELECT table_name, column_name, udt_name FROM information_schema.columns
WHERE column_name IN ('role','platform_role')
  AND table_name IN ('useraccessgrouplink','resourceacl','user');
```

If any says `character varying`, someone ran `SQLModel.metadata.create_all()`
against an empty database first and `ADD COLUMN IF NOT EXISTS` silently
skipped. Drop the columns and re-run.

### Phase 1b — deploy the code

```bash
git push origin feat/permission-roles:staging     # or main, once merged
```

Wait for the rollout. **This must land before the backfill** (ordering rule 1).

### Phase 1c — backfill

```bash
DB_CONF_FILE=.env python migrations/data/20260806_backfill_permission_roles.py            # dry run
DB_CONF_FILE=.env python migrations/data/20260806_backfill_permission_roles.py --commit
```

Dry run first, always. It self-verifies and rolls back if anything is left
null, any group ends up with two owners, or a `project_id` collides with
another resource type.

Expect it to promote **every admin-group member, service accounts included**,
and to say so loudly. That is deliberate: withholding `platform_role` from them
means they hold nothing at cutover and every instrument stops writing.
Narrowing them is tracked separately — see "Regressing service account rights"
in the plan.

### Verify

```sql
SELECT role, count(*) FROM useraccessgrouplink GROUP BY 1;   -- no NULLs
SELECT role, count(*) FROM resourceacl GROUP BY 1;           -- no NULLs
SELECT count(*) FROM resourceacl a JOIN accessgroup ag ON ag.id=a.accessgroup_id
  WHERE ag.group_name IN ('admin','read_only');              -- 0
SELECT count(*) FROM idtype WHERE resource_type='project';   -- = project count
SELECT indexname FROM pg_indexes WHERE indexname='one_owner_per_group';
```

Staging after backfill, for comparison: 12,311 `editor` / 5,426 `owner` /
29 `viewer` standings; 18,048 `owner` / 11,291 `admin` / 2,588 `contributor` /
2 `viewer` grants; 6,756 project resources; 31,929 ACL rows total, down from
47,614.

Then confirm an admin-group service account still works — this is what
ordering rule 1 protects:

```python
from app.utils.auth_utils import require_read_access   # must not raise
```

---

## Group C rollout (staging)

Instrument identity, instrument group lifecycle, `AccessGroup` kind columns,
project-group MFID rename. Code is written; nothing below has run yet. Run
every step in order — steps 2 and 5 are hard prerequisites for code already
in the branch, not optional cleanup: deploying before them breaks dataset
creation (step 2) or every project route (step 5).

```bash
# 1. schema
DB_CONF_FILE=.env alembic upgrade head

# 2. instrument operator groups — REQUIRED before deploy, or create_dataset
#    500s for every existing instrument (no MFID-keyed group yet)
DB_CONF_FILE=.env python migrations/data/20260810_provision_instrument_operator_groups.py
DB_CONF_FILE=.env python migrations/data/20260810_provision_instrument_operator_groups.py --commit

# 3. instrument_id slugs — additive, no ordering constraint
DB_CONF_FILE=.env python migrations/data/20260810_backfill_instrument_id.py --commit

# 4. project unique_id, then the group rename it unblocks — REQUIRED before
#    deploy, or every project route 403s/404s (no fallback path, unlike rule 1)
DB_CONF_FILE=.env python migrations/data/20260810_backfill_project_unique_id.py --commit
DB_CONF_FILE=.env python migrations/data/20260810_rename_project_groups_to_mfid.py
DB_CONF_FILE=.env python migrations/data/20260810_rename_project_groups_to_mfid.py --commit

# 5. only now, deploy
git push origin <branch>:staging
```

Dry run (no `--commit`) before every `--commit` line above — all four scripts
default to dry-run and print what they'd do. Watch for `WARNING:` lines in
steps 2 and 4: a legacy group name shared with something else (a user's
ORCID, another project/instrument) is left untouched and needs a manual call,
not a rerun.

Verify:

```sql
SELECT count(*) FROM instrument WHERE instrument_id IS NULL;              -- 0
SELECT count(*) FROM project WHERE unique_id IS NULL;                     -- 0
```

`AccessGroup` briefly gained `owner_orcid`/`resource_mfid` classification
columns here (migration `8ca4a71f1bed`) but they were reverted in
`bb780697b63b` after an audit found no real reader — omitted from the steps
above since `alembic upgrade head` already carries both the add and the
drop for anyone running this fresh. See docs/permission_system_plan.md,
"Membership lived in two tables," for the full story.

---

## Project/instrument identity rollout (staging)

Repoints `Dataset.project_id`/`Sample.project_id` and `Dataset.instrument_name`
onto `Project.unique_id`/`Instrument.unique_id` (real FKs, `project_mfid`/
`instrument_mfid` columns), instead of the mutable human labels. Same
expand-only discipline as Group C: old columns stay, nothing is dropped.
Depends on Group C above already being live (project/instrument `unique_id`
must be populated and their groups already renamed).

```bash
# 1. schema: adds dataset.project_mfid, sample.project_mfid, dataset.instrument_mfid
DB_CONF_FILE=.env alembic upgrade head

# 2. project_mfid backfill (dataset + sample) - dangling project_id values are
#    expected (a known pre-existing data-quality issue for Sample.project_id,
#    which never had a real FK before) and are logged, not treated as errors
DB_CONF_FILE=.env python migrations/data/20260811_backfill_project_mfid.py
DB_CONF_FILE=.env python migrations/data/20260811_backfill_project_mfid.py --commit

# 3. instrument_mfid backfill (dataset only)
DB_CONF_FILE=.env python migrations/data/20260811_backfill_instrument_mfid.py
DB_CONF_FILE=.env python migrations/data/20260811_backfill_instrument_mfid.py --commit

# 4. only now, deploy
git push origin <branch>:staging
```

Dry run (no `--commit`) before each `--commit` line above. Both scripts print
progress every 500 rows and a final resolved/dangling count. A dangling row
(project_id or instrument_id with no match) is left `NULL`, not blocked.

Verify:

```sql
SELECT count(*) FROM dataset WHERE project_id IS NOT NULL AND project_mfid IS NULL;      -- expected dangling count from step 2, not 0
SELECT count(*) FROM sample WHERE project_id IS NOT NULL AND project_mfid IS NULL;        -- same
SELECT count(*) FROM dataset WHERE instrument_id IS NOT NULL AND instrument_mfid IS NULL; -- expected dangling count from step 3
```

Re-run steps 2 and 3 after the code deploys (both are idempotent) to pick up
anything created in the gap between the backfill and the deploy landing.

---

## Project's own resource identity + join-request FK (staging)

Two independent fixes to identity that stayed genuinely mutable-label-keyed
after everything above shipped:

- `create_project` seeded `Resource.mfid`/`ResourceACL.mfid` for a project's
  own idtype/ACL row with `project_id`, not `Project.unique_id` — the same
  bug already fixed for Dataset/Sample/Instrument FKs, just missed for a
  project's identity toward itself. Every `require_permission`/`require_grant`
  call site and generic `/resources/{mfid}/...` route that special-cases
  projects moves to `Project.unique_id` in the same change.
- `AccessGroupJoinRequest.group_name` FK'd to `accessgroup.group_name`, which
  has meant a project's MFID since the C4b group rename — but
  `app/api/routes/access_groups.py` wrote/read it as a project_id throughout.
  Live-broken since C4b (any real insert should violate the FK); no test
  exercised it against an FK-enforcing database. Fixed by switching to
  `accessgroup_id: int` (an internal id, no label/MFID ambiguity to get
  wrong). 0 rows on staging, confirmed before writing the migration, so no
  data backfill needed for this one — a straight schema change.

```bash
# 1. schema: accessgroupjoinrequest.group_name -> accessgroup_id
DB_CONF_FILE=.env alembic upgrade head

# 2. data: repoint existing projects' idtype/resourceacl rows from
#    project_id to unique_id (dry run first, both roll back cleanly if
#    anything looks wrong)
DB_CONF_FILE=.env python migrations/data/20260813_repoint_project_resource_identity.py
DB_CONF_FILE=.env python migrations/data/20260813_repoint_project_resource_identity.py --commit

# 3. only now, deploy
git push origin <branch>:staging
```

Step 2 must run before step 3 lands: the code no longer checks ACL rows
keyed by `project_id` at all, so a project not yet repointed would fail
every permission check on itself until the backfill catches it up. No
re-run-after-deploy step is needed here (unlike the project/instrument
identity rollout above) — `create_project` writes the correct identity from
the moment the new code is live, there's no gap to backfill.

Verify:

```sql
SELECT count(*) FROM project p JOIN idtype i ON i.mfid = p.project_id WHERE i.resource_type = 'project';  -- expected 0 after step 2
SELECT count(*) FROM project p JOIN idtype i ON i.mfid = p.unique_id WHERE i.resource_type = 'project';   -- expected = total project count
```

---

## Project rename support (staging)

Builds directly on the identity fix above: `PATCH /projects/{proj_id}` now
accepts `project_id`, and every dataset/sample read path resolves
`project_id` live from `project_mfid` rather than trusting the text stored
at creation. Pure code, no schema or data migration — deploy like any other
release, no ordering constraint with anything else in this document.

---

## Before cutover (phase 4)

Not optional. Cutover switches authorization to `platform_role` and
`effective_role`, at which point:

- **Service accounts survive it**, because the backfill gave them
  `platform_role='admin'`. That is the whole reason for the promotion. Their
  ACL-derived role is `contributor` on their own instrument's datasets and
  nothing else — visible via `effective_role`, which ignores the bypass.
- **No human holds `platform_role='admin'`.** The only accounts that can bypass
  anything are instrument credentials. Worth naming a human before cutover so
  there is a break-glass path that is not a machine.

After cutover, narrow the service accounts one instrument at a time: bind →
confirm the directory route → set `platform_role='none'` → watch ingestion →
remove from the admin group. See "Regressing service account rights" in the
plan. Until that is done, a compromised instrument credential reaches every
resource.

---

## Rollback

| Phase | Rollback |
|---|---|
| 1a schema | drop the added columns and types |
| 1b code | revert the deploy |
| 1c backfill | re-runnable and convergent; `admin`/`read_only` ACL rows are regenerable from group membership |
| 3 shadow | flag off |
| 4 cutover | code revert — old columns still present and populated |
| 5 contract | **restore from backup** — this is why it ships last and alone |

Take a Cloud SQL snapshot before 1c on production.

---

## Related documents

| Document | Contents |
|---|---|
| `docs/permission_system_plan.md` | the design: roles, `MIN`/`MAX`, groups, decisions and their reasoning |
| `docs/schema_hygiene.md` | FK and identifier problems this work exposed but does not fix |
| `~/crucible-audits/dangling-sample-projects.md` | the 567 deleted samples, with the record of what was deleted |
| `~/crucible-audits/empty-project-datasets/` | the 1,292 datasets under the nameless project, and the uploader bug behind it |
