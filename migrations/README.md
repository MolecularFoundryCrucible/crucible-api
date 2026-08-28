# Database migrations

Use Alembic for schema changes and `migrations/data/` for reviewed data changes.

## Schema changes

Alembic revisions live in `migrations/versions/`. Generate and review a revision before applying it:

```bash
DB_CONF_FILE=.env .venv/bin/alembic revision --autogenerate -m "describe the change"
DB_CONF_FILE=.env .venv/bin/alembic upgrade head
```

Production commands require the explicit safety flag:

```bash
DB_CONF_FILE=.env .venv/bin/alembic -x yes-this-is-production=true upgrade head
```

## Data changes

Backfills, audits, and one-time cleanup scripts live in `migrations/data/`. Scripts that write data default to a dry run and require `--commit`. Production writes also require `--yes-this-is-production`.

Run the permission migration only from `docs/permission_migration_runbook.md`. It contains required application deployment checkpoints between schema and data steps, so a plain `alembic upgrade head` is not a safe substitute.

Revision `91f3c8a4d2e7` adds nullable `accessgroup.group_type` from the additive migration checkpoint. Apply it before running the project and instrument identity scripts. Audit and reconcile legacy groups with `20260827_audit_legacy_access_groups.py` and `20260827_merge_legacy_entity_groups.py`, then run `20260827_backfill_access_group_types.py`. Revision `c4a7e9d2b615` merges that branch with the main permission migration and makes the column non-null.

Revision `7b2d4e6f8a10` is the permission-invariant enforcement step. It refuses to run while either role column contains nulls or any resource has multiple owner ACLs. Revision `c4a7e9d2b615` is the final permission migration head. Both enforcement revisions are transactional and must run with writes paused unless compatible writers are already fully deployed.

Run `20260828_audit_permission_cutover_preconditions.py` immediately before final enforcement. It is read-only and checks every data invariant required by revisions `7b2d4e6f8a10` and `c4a7e9d2b615`.

Revision `454c96264cfb` preserves existing access-group join requests while replacing `group_name` with `accessgroup_id`. Staging applied the same final schema when the table was empty; production may contain rows and uses the data-preserving path.
