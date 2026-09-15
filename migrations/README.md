# Database migrations

Use Alembic revisions in `migrations/versions/` for schema changes and reviewed scripts in `migrations/data/` for audits, backfills, and one-time data cleanup.

## Schema changes

Generate and review a revision before applying it:

```bash
DB_CONF_FILE=.env.local .venv/bin/alembic revision --autogenerate -m "describe the change"
DB_CONF_FILE=.env.local .venv/bin/alembic upgrade head
```

Production commands require the explicit safety flag, including read-only Alembic commands:

```bash
DB_CONF_FILE=.env.prod \
.venv/bin/alembic -x yes-this-is-production=true current

DB_CONF_FILE=.env.prod \
.venv/bin/alembic -x yes-this-is-production=true upgrade head
```

Before a production migration, confirm the target database, take the required backup, pause incompatible writers, inspect the revision chain, and run any revision-specific preconditions. Do not assume `upgrade head` is safe when a migration has ordered data steps or application deployment checkpoints.

## Data changes

Data scripts should default to read-only or rollback behavior. Review their output before using `--commit`. Production writes additionally require `--yes-this-is-production` when the script supports that guard.

```bash
DB_CONF_FILE=.env.prod \
.venv/bin/python migrations/data/<script>.py \
  --yes-this-is-production

DB_CONF_FILE=.env.prod \
.venv/bin/python migrations/data/<script>.py \
  --commit \
  --yes-this-is-production
```

The August 2026 permission rollout is complete. Its ordered procedure is retained only as a [historical runbook](../docs/archive/permission_migration_runbook_2026-08.md). Do not replay it against a current environment. Accepted production cleanup items remain in [production migration data follow-ups](../docs/production_migration_data_followups.md).

## Resource timestamp migration

Revision `8c4f2a1d7e60` directly converts the API-owned `idtype.creation_time` and `idtype.modification_time` fields to PostgreSQL `TIMESTAMPTZ`. Revision `3f2b6a9c1d84` adds database enforcement for generated creation and modification times, immutable creation times, and automatically advanced modification times. Follow [the resource timestamp migration runbook](../docs/resource_timestamp_online_migration_runbook.md) to coordinate the migration with the main application rollout while production pods remain running.

Before any production schema write, run the read-only audit:

```bash
DB_CONF_FILE=.env.prod \
.venv/bin/python migrations/data/20260908_audit_resource_timestamps.py
```

The audit reports every approved legacy format separately. Dashed and compact date-only values use midnight in `America/Los_Angeles`; `_am` uses midnight and `_pm` uses noon in that zone; `YYYY-MM-DD_HHMMSS` preserves its encoded local time; and other timezone-less values use the same zone. Empty strings and `NaN` become null. A missing modification time uses creation time when one exists and otherwise remains null. Explicitly offset values preserve their instants. Unsupported non-null values block migration. The producer-supplied `dataset.timestamp` remains unconstrained text and is not normalized or used to repair either lifecycle timestamp.

The audit script does not mutate data. Production pods remain running during the direct schema migration and rolling application deployment. Do not scale or delete production pods as part of this migration without a separately approved maintenance plan.
