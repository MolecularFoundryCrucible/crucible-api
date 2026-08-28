# Permission Migration Runbook

This is the operational recipe for the permission migration. Follow it in order. Use `docs/permission_system_plan.md` for design details and `docs/permission_system_api_changes.md` for client-facing contract changes.

## Current status

Verified on 2026-08-28.

| Environment | Status |
|---|---|
| Staging database | Final permission head `c4a7e9d2b615` is applied. All 9,880 access groups are classified, permission roles are non-null, duplicate owner ACLs are rejected, and the duplicate-owner `unknown` group has been removed. |
| Staging API | The final API release is being deployed from `origin/staging`. Treat the public OpenAPI as authoritative only after the deployment completes and the verification below passes. |
| Production | Not queried during this review. The existing rollout record says the migration has not started. Re-run every preflight check against production before rollout. |

All counts in the staging sections describe staging only. Production is substantially larger, so do not use staging row counts as production expectations or anomaly thresholds. For production, compare each dry run with its immediately following committed run and enforce only the documented structural invariants and zero-result blockers.

### Staging checks that passed

- Alembic is at final head `c4a7e9d2b615`.
- `resourceacl.permission` exists and the old `resourceacl.role` column is gone.
- `accessgroupjoinrequest.accessgroup_id` exists and the old `group_name` column is gone.
- All 6,756 projects have `idtype` and ACL rows keyed by `Project.unique_id`.
- No dataset or sample with a valid project reference is missing `project_mfid`.
- No dataset with a valid instrument reference is missing `instrument_mfid`.
- `Project.unique_id` and `Instrument.unique_id` have unique indexes.
- No membership role is null.
- No ACL permission is null.
- No access group has more than one owner.
- The read-only precondition audit reports no blockers.
- Access-group classification committed 3,031 identity, 6,756 project, 43 instrument, 48 custom, and 3 system groups. Removing `unknown` afterward leaves 47 custom groups and 9,880 groups total.
- The final cutover precondition audit passes with zero null ACL roles, zero null membership roles, zero duplicate owner ACLs, and zero unclassified groups.

Revision `7b2d4e6f8a10` hardened role invariants, and revision `c4a7e9d2b615` merged both branches and enforced non-null group types. Both are applied on staging.

### Staging follow-ups

Staging is structurally migrated. These accepted operational follow-ups remain:

1. Of 6,756 projects, 4,362 have no owner member in their project group. For 3,049 of them the recorded lead is a registered user but is not a group member. The remaining 1,313 name an unregistered lead. This historical state is accepted for the current staging rollout and does not block deployment. Any later owner assignment changes authorization and requires a separately reviewed policy and data operation.
2. Seven service accounts still have `platform_role='admin'`, and no human has that role. This preserved ingestion during cutover. The absence of a human platform administrator is accepted for the current staging rollout. Assigning a human administrator and removing service-account bypasses remain later operational work after instrument bindings are verified.
3. The confirmed duplicate-owner `unknown` group was removed with `20260828_remove_staging_unknown_group.py`, including its owner ACL and membership. Forty-seven inert custom groups remain for case-by-case cleanup: four empty ORCID-shaped orphans and 43 other groups with no ACLs or join requests.

None of these accepted follow-ups blocks the current API deployment.

The malformed ACL state was repaired with:

```bash
DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_repair_staging_null_sample_acl.py
DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_repair_staging_null_sample_acl.py --commit
```

Both runs reported `crucible-staging`. The personal group received `owner`, the project group received `admin`, and the obsolete `admin` and `read_only` grants were deleted. Independent verification returned `0` for `SELECT count(*) FROM resourceacl WHERE permission IS NULL;` and exactly those two grants for sample `0tkpgy57k5w9h000swxzek7wrr`. Do not rerun `20260806_backfill_permission_roles.py` against the current schema because that script targets the old `resourceacl.role` column.

The staging deployment candidate removes dataset `source_folder`, makes ownership exclusive to the transfer route, writes explicit access-group types, and includes the route and authorization changes documented in `docs/permission_system_api_changes.md`. These contracts are not live until the deployment completes.

### Pending staging deployment and verification

The snapshot, schema migrations, legacy-group review, classification commit, `unknown` cleanup, and precondition audit are complete. Forty-seven inert groups remain custom until case-by-case cleanup. Keep staging writes and ingestion paused until these remaining steps finish:

1. Deploy the API release that writes explicit group types.
2. Run the verification queries and smoke-test creation, publication, ACL changes, project membership, ownership transfer, and instrument ingestion.
3. Resume staging writes and ingestion only after verification passes.

The precondition audit checks null ACL roles, null membership roles, duplicate owner ACLs, and unclassified groups. It is read-only and supports both the pre-rename `resourceacl.role` schema and the post-rename `resourceacl.permission` schema.

Verification:

```sql
SELECT is_nullable, udt_name
FROM information_schema.columns
WHERE table_name = 'accessgroup' AND column_name = 'group_type';

SELECT table_name, column_name, is_nullable
FROM information_schema.columns
WHERE (table_name, column_name) IN (('resourceacl', 'permission'), ('useraccessgrouplink', 'role'))
ORDER BY table_name, column_name;

SELECT count(*)
FROM pg_indexes
WHERE tablename = 'resourceacl' AND indexname = 'one_owner_acl_per_resource';

SELECT group_type, count(*)
FROM accessgroup
GROUP BY group_type
ORDER BY group_type;

SELECT count(*)
FROM (
    SELECT ag.id
    FROM accessgroup ag
    JOIN "user" u ON u.orcid = ag.group_name
    LEFT JOIN useraccessgrouplink l ON l.accessgroup_id = ag.id
    WHERE ag.group_type = 'identity'
    GROUP BY ag.id, u.id
    HAVING count(l.user_id) <> 1
       OR count(*) FILTER (WHERE l.user_id = u.id AND l.role = 'owner') <> 1
) malformed_identity_groups;
```

Expected results: `group_type` is non-null and uses `access_group_type`; both permission columns are non-null; the owner index count is `1`; `malformed_identity_groups` is `0`. Group counts are informational and depend on the environment.

## Safety rules

1. Confirm the database name before every command. Staging and production share a Cloud SQL instance.
2. Run every data script without `--commit` first. Review its target database, counts, and warnings before committing.
3. Take a Cloud SQL snapshot before the permission backfill and before the final hard cutover.
4. Stop if a verification query does not match its expected result.
5. Keep the final schema migration and final application deploy in one maintenance window. The old application cannot read the renamed columns, and the new application cannot run before those columns are renamed.

## Deployment checkpoints

Production requires staged application builds. Do not deploy the current branch tip at the start of the migration.

| Checkpoint | Commit | Purpose |
|---|---|---|
| Expand-compatible build | `e75185b` | Reads the additive role columns, keeps old authorization active, and includes the admin-group bypass needed before ACL cleanup. |
| Identity build | `737d81e` | Uses MFID-keyed project groups and instrument operator groups after their backfills. |
| Final build | reviewed release commit | Uses `resourceacl.permission`, `accessgroupjoinrequest.accessgroup_id`, explicit role writers, and the completed identity model. |

Build and identify all three deployable artifacts before changing production data.

## Production recipe

### 1. Connect and confirm the target

Start the proxy:

```bash
cloud-sql-proxy --port 5433 mf-crucible:us-central1:crucible-db
```

Configure `.env` for the intended database and always pass it explicitly:

```bash
export DB_CONF_FILE=.env
```

Confirm that production is selected:

```sql
SELECT current_database();
```

Expected result: `crucible`.

### 2. Resolve production preconditions

Run the read-only audit:

```bash
.venv/bin/python migrations/data/20260806_audit_permission_preconditions.py
```

The last production audit found one orphaned `idtype` row with an empty MFID. Confirm that it is still an orphan before deleting it:

```sql
SELECT i.*
FROM idtype i
LEFT JOIN sample s ON s.unique_id = i.mfid
WHERE i.mfid = '' AND i.resource_type = 'sample' AND s.id IS NULL;
```

If and only if that query returns the known orphan, delete it:

```sql
DELETE FROM idtype WHERE mfid = '' AND resource_type = 'sample';
```

Run the audit again. Continue only when it reports no blockers.

### 3. Take the first snapshot

Take a Cloud SQL snapshot immediately before the first production write. Record its name and timestamp in the rollout ticket.

### 4. Add the permission columns

```bash
.venv/bin/python migrations/data/20260806_apply_sql.py migrations/data/20260806_add_permission_roles.sql --yes-this-is-production
```

Verify the new columns use PostgreSQL enum types:

```sql
SELECT table_name, column_name, udt_name
FROM information_schema.columns
WHERE table_name IN ('useraccessgrouplink', 'resourceacl', 'user')
  AND column_name IN ('role', 'platform_role');
```

Do not continue if either column is `character varying`.

### 5. Deploy the expand-compatible build

Deploy commit `e75185b`. Wait for a healthy rollout and verify authenticated reads and ingestion before continuing.

This deploy must happen before the permission backfill removes the old `admin` and `read_only` ACL rows.

### 6. Backfill roles and ACL grants

Run the dry run:

```bash
.venv/bin/python migrations/data/20260806_backfill_permission_roles.py --yes-this-is-production
```

Review all counts and warnings. Then commit:

```bash
.venv/bin/python migrations/data/20260806_backfill_permission_roles.py --commit --yes-this-is-production
```

Verify:

```sql
SELECT count(*) FROM useraccessgrouplink WHERE role IS NULL;
SELECT count(*) FROM resourceacl WHERE role IS NULL;
SELECT count(*) FROM resourceacl a JOIN accessgroup g ON g.id = a.accessgroup_id WHERE g.group_name IN ('admin', 'read_only');
SELECT count(*) FROM (SELECT accessgroup_id FROM useraccessgrouplink WHERE role = 'owner' GROUP BY accessgroup_id HAVING count(*) > 1) duplicate_owners;
```

Expected result for every query: `0`.

Verify authenticated reads, project access, and ingestion again.

### 7. Adopt Alembic and apply additive revisions

The baseline is empty because step 4 already created the initial permission schema:

```bash
.venv/bin/alembic -x yes-this-is-production=true stamp fc9be1ddfb92
.venv/bin/alembic -x yes-this-is-production=true upgrade 91f3c8a4d2e7
```

This follows the additive chain through `e5d68f53b42c`, then adds nullable `accessgroup.group_type` on its migration branch. It stops before the incompatible column renames and the non-null enforcement.

### 8. Prepare project and instrument identity data

Run each script without `--commit`, review the output, and then rerun it with `--commit`:

```bash
.venv/bin/python migrations/data/20260810_provision_instrument_operator_groups.py --yes-this-is-production
.venv/bin/python migrations/data/20260810_provision_instrument_operator_groups.py --commit --yes-this-is-production

.venv/bin/python migrations/data/20260810_backfill_instrument_id.py --yes-this-is-production
.venv/bin/python migrations/data/20260810_backfill_instrument_id.py --commit --yes-this-is-production

.venv/bin/python migrations/data/20260810_backfill_project_unique_id.py --yes-this-is-production
.venv/bin/python migrations/data/20260810_backfill_project_unique_id.py --commit --yes-this-is-production

.venv/bin/python migrations/data/20260810_rename_project_groups_to_mfid.py --yes-this-is-production
.venv/bin/python migrations/data/20260810_rename_project_groups_to_mfid.py --commit --yes-this-is-production

.venv/bin/python migrations/data/20260827_audit_legacy_access_groups.py
.venv/bin/python migrations/data/20260827_merge_legacy_entity_groups.py --yes-this-is-production
.venv/bin/python migrations/data/20260827_merge_legacy_entity_groups.py --commit --yes-this-is-production

.venv/bin/python migrations/data/20260827_backfill_access_group_types.py
.venv/bin/python migrations/data/20260827_backfill_access_group_types.py --commit --accept-custom-groups --accept-suspicious-custom-groups --yes-this-is-production
```

Production counts are expected to differ substantially from staging. Review classifications and warnings based on production data; do not compare their totals with the staging record.

Verify:

```sql
SELECT count(*) FROM instrument WHERE instrument_id IS NULL;
SELECT count(*) FROM project WHERE unique_id IS NULL;
SELECT count(*) FROM accessgroup WHERE group_type IS NULL;
SELECT count(*) FROM accessgroup ag JOIN "user" u ON u.orcid = ag.group_name WHERE ag.owner_orcid IS DISTINCT FROM u.orcid;
```

Expected result for every query: `0`. The legacy-group audit must contain only reviewed intentional custom groups before deploying the identity build.

### 9. Deploy the identity build

Deploy commit `737d81e`. Wait for a healthy rollout. Verify project reads, project membership operations, dataset creation for registered instruments, and ingestion.

### 10. Backfill resource identity references

Run each script first as a dry run and then with `--commit`:

```bash
.venv/bin/python migrations/data/20260811_backfill_project_mfid.py --yes-this-is-production
.venv/bin/python migrations/data/20260811_backfill_project_mfid.py --commit --yes-this-is-production

.venv/bin/python migrations/data/20260811_backfill_instrument_mfid.py --yes-this-is-production
.venv/bin/python migrations/data/20260811_backfill_instrument_mfid.py --commit --yes-this-is-production
```

Valid references must have an MFID. Dangling legacy labels may remain null and must be reviewed separately:

```sql
SELECT count(*) FROM dataset d JOIN project p ON p.project_id = d.project_id WHERE d.project_id IS NOT NULL AND d.project_mfid IS NULL;
SELECT count(*) FROM sample s JOIN project p ON p.project_id = s.project_id WHERE s.project_id IS NOT NULL AND s.project_mfid IS NULL;
SELECT count(*) FROM dataset d JOIN instrument i ON lower(i.instrument_name) = lower(d.instrument_name) WHERE d.instrument_name IS NOT NULL AND d.instrument_mfid IS NULL;
```

Expected result for every query: `0`.

### 11. Prepare the final maintenance window

Before starting:

- Build and smoke-test the final release artifact.
- Schedule a maintenance window.
- Pause writes and ingestion.
- Take a second Cloud SQL snapshot.
- Run the project identity repair as a dry run:

```bash
.venv/bin/python migrations/data/20260813_repoint_project_resource_identity.py --yes-this-is-production
.venv/bin/python migrations/data/20260827_audit_legacy_access_groups.py
.venv/bin/python migrations/data/20260827_merge_legacy_entity_groups.py --yes-this-is-production
.venv/bin/python migrations/data/20260827_backfill_access_group_types.py --yes-this-is-production
.venv/bin/python migrations/data/20260828_audit_permission_cutover_preconditions.py
```

The access-group classifier must report no identifier collisions, missing entity-owned groups, conflicting stored types, or malformed identity groups. Record any accepted suspicious custom groups for later case-by-case cleanup.

### 12. Run the hard cutover and deploy

Keep these commands and the deploy in the same maintenance window:

```bash
.venv/bin/python migrations/data/20260827_merge_legacy_entity_groups.py --commit --yes-this-is-production
.venv/bin/python migrations/data/20260827_backfill_access_group_types.py --commit --accept-custom-groups --accept-suspicious-custom-groups --yes-this-is-production
.venv/bin/python migrations/data/20260828_audit_permission_cutover_preconditions.py
.venv/bin/python migrations/data/20260813_repoint_project_resource_identity.py --commit --yes-this-is-production
.venv/bin/alembic -x yes-this-is-production=true upgrade c4a7e9d2b615
```

Immediately deploy the final release artifact.

Resume traffic only after the application is healthy and the verification in step 14 passes.

### 13. Catch writes from the rollout gaps

After the final application is live, rerun the identity scripts as dry runs. Commit only if they report rows created during an earlier deployment gap:

```bash
.venv/bin/python migrations/data/20260811_backfill_project_mfid.py --yes-this-is-production
.venv/bin/python migrations/data/20260811_backfill_instrument_mfid.py --yes-this-is-production
.venv/bin/python migrations/data/20260813_repoint_project_resource_identity.py --yes-this-is-production
```

### 14. Verify the final state

```bash
.venv/bin/alembic current
```

Expected revision: `c4a7e9d2b615 (head)`.

Run these database checks:

```sql
SELECT count(*) FROM information_schema.columns WHERE table_name = 'resourceacl' AND column_name = 'role';
SELECT count(*) FROM information_schema.columns WHERE table_name = 'resourceacl' AND column_name = 'permission';
SELECT count(*) FROM information_schema.columns WHERE table_name = 'accessgroupjoinrequest' AND column_name = 'group_name';
SELECT count(*) FROM information_schema.columns WHERE table_name = 'accessgroupjoinrequest' AND column_name = 'accessgroup_id';
SELECT count(*) FROM useraccessgrouplink WHERE role IS NULL;
SELECT count(*) FROM resourceacl WHERE permission IS NULL;
SELECT count(*) FROM (SELECT mfid FROM resourceacl WHERE permission = 'owner' GROUP BY mfid HAVING count(*) > 1) duplicate_owner_acls;
SELECT count(*) FROM pg_indexes WHERE tablename = 'resourceacl' AND indexname = 'one_owner_acl_per_resource';
SELECT count(*) FROM resourceacl a JOIN accessgroup g ON g.id = a.accessgroup_id WHERE g.group_name IN ('admin', 'read_only');
SELECT count(*) FROM project p LEFT JOIN idtype i ON i.mfid = p.unique_id AND i.resource_type = 'project' WHERE i.mfid IS NULL;
SELECT count(*) FROM project p WHERE NOT EXISTS (SELECT 1 FROM resourceacl a WHERE a.mfid = p.unique_id);
SELECT count(*) FROM accessgroup WHERE group_type IS NULL;
SELECT count(*) FROM (
    SELECT ag.id
    FROM accessgroup ag
    JOIN "user" u ON u.orcid = ag.group_name
    LEFT JOIN useraccessgrouplink l ON l.accessgroup_id = ag.id
    WHERE ag.group_type = 'identity'
    GROUP BY ag.id, u.id
    HAVING count(l.user_id) <> 1
       OR count(*) FILTER (WHERE l.user_id = u.id AND l.role = 'owner') <> 1
) malformed_identity_groups;
```

Expected results in order: `0`, `1`, `0`, `1`, `0`, `0`, `0`, `1`, `0`, `0`, `0`, `0`, `0`.

Repeat the valid-reference checks from step 10. Then smoke-test:

- authenticated dataset, sample, project, and instrument reads
- dataset and sample creation
- project membership changes
- ACL grant and revoke operations
- ownership transfer preview and confirmation
- instrument ingestion

## After the migration

These are separate follow-up changes, not reasons to extend the production maintenance window:

1. Assign at least one human `platform_role='admin'` as a break-glass administrator.
2. Bind each service account to its instrument, verify ingestion and project discovery, set its `platform_role` to `none`, and remove it from the legacy admin group.
3. Decide how to handle historical projects whose recorded lead is not an active owner member.
4. Leave `resourceacl.read`, `resourceacl.write`, `dataset.public`, and `idtype.public` in place until the new authorization path has been stable long enough to make rollback unlikely. Drop them only in a separate contract migration with a fresh backup.

## Rollback

| Point | Recovery |
|---|---|
| Before step 6 | Revert the application deploy and remove the additive columns only if necessary. |
| After step 6 and before step 12 | Revert to the last compatible application build. The old columns remain available. |
| During step 12 | Keep traffic paused. Either complete the final deploy or restore the second snapshot and redeploy the identity build. |
| After `7b2d4e6f8a10`, before resuming writes | Downgrade to `ecfa70615e54` while writes remain paused if an old writer must be restored. The downgrade removes the resource-owner index and makes the role columns nullable again; it does not alter existing role values. |
| After `c4a7e9d2b615`, before resuming writes | Downgrade the merge revision while writes remain paused to make `group_type` nullable again. Restore the snapshot if the old application must run without the access-group column or if classification data must be reverted. |
| After old compatibility columns are eventually dropped | Restore from backup. A code revert is no longer sufficient. |

## Related documents

| Document | Purpose |
|---|---|
| `docs/permission_system_plan.md` | Permission model, invariants, and design decisions. |
| `docs/permission_system_api_changes.md` | Client-facing route and schema changes. |
| `migrations/README.md` | Where schema revisions and data scripts belong. |
