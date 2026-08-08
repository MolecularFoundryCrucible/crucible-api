# Permission Migration Runbook

Operational companion to `docs/permission_system_plan.md`. That document is the
design — what the model is and why. This one is what to type, in what order,
and what will bite you.

Staging is through phases 0–3. Production has not started. Everything below
reflects what actually happened rather than what was planned, including the
ordering mistakes made along the way.

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

**Branch:** `feat/permission-roles`, 22 commits, **not merged to `main`**.
Deployed to staging by pushing `feat/permission-roles:staging` (fast-forward).
Test suite: 475 hermetic, 549 with a throwaway Postgres.

### What has been built

| Area | Where |
|---|---|
| Pure permission model — `Role`, `Principal`, `effective_role`, `has_permission`, `can_grant` | `app/utils/permissions.py` |
| Auth returns a `Principal`; admin-group bypass; public-group injection | `app/utils/auth_utils.py` |
| Shadow mode | `app/utils/shadow.py` |
| Precondition audit (read-only) | `migrations/audit_permission_preconditions.py` |
| Additive schema | `migrations/add_permission_roles.sql` |
| Backfill (dry-run default, idempotent, self-verifying) | `migrations/backfill_permission_roles.py` |
| Batched delete of the 721k bypass rows | `migrations/delete_bypass_acl_rows.py` |
| `.sql` runner using the app's own DB config | `migrations/apply_sql.py` |
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
DB_CONF_FILE=.env python migrations/audit_permission_preconditions.py
```

Read-only. Four counts: public-column drift, dangling `Sample.project_id`,
`read`/`write` distribution, multi-member ORCID groups. Resolve anything
non-zero before continuing.

### Phase 1a — schema

```bash
DB_CONF_FILE=.env python migrations/apply_sql.py migrations/add_permission_roles.sql
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
DB_CONF_FILE=.env python migrations/backfill_permission_roles.py            # dry run
DB_CONF_FILE=.env python migrations/backfill_permission_roles.py --commit
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
