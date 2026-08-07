# Permission Migration Runbook

Operational companion to `docs/permission_system_plan.md`. That document is the
design — what the model is and why. This one is what to type, in what order,
and what will bite you.

Staging has been through phases 0–2. Production has not started. Everything
below reflects what actually happened on staging rather than what was planned,
including two ordering mistakes made in the process.

---

## Status

| Phase | Staging | Production |
|---|---|---|
| 0 — green test suite | done | n/a (repo-wide) |
| 0.5 — precondition audit | done, clean | done, **2 blockers found** |
| 1 — expand schema | done | not started |
| 1 — backfill | done | not started |
| 2 — inert code (`Principal`, `effective_role`) | deployed `f03838e` | not deployed |
| 3 — shadow mode | n/a (no organic traffic) | not started |
| 4 — cutover | not started | not started |
| 5 — contract (drops) | not started | not started |

**Branch:** `feat/permission-roles`, not merged to `main`. Deployed to staging
by pushing `feat/permission-roles:staging` (fast-forward).

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

Expect it to report **0 humans promoted** and list the service accounts it
skipped. That is correct: no human platform admins exist yet, by decision.

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

- **Every service account loses access.** On staging their `admin` standing is
  `viewer` and `platform_role` is `none`, so the new check resolves them to
  nothing. Verified directly. They must be bound to their instruments first,
  via the binding route added in phase 4.
- **Nobody can bypass anything.** No human holds `platform_role='admin'`.
  Name some explicitly, or there is no break-glass path.

Sequence: name human platform admins → add the SA binding routes → bind each
service account → relax `GET /users/{orcid}/projects` with the instrument-group
branch → remove the service accounts from the admin group → verify ingestion →
then cut over.

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
