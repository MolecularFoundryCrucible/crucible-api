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

Revisions `8c4f2a1d7e60` and `3f2b6a9c1d84` converted API-owned lifecycle timestamps to PostgreSQL `TIMESTAMPTZ` and added database enforcement. Production completed this migration on 2026-09-15. Current behavior is documented in the [resource timestamp policy](../docs/resource_timestamp_policy.md), and the rollout evidence is retained in the [archived migration record](../docs/archive/resource_timestamp_migration_2026-09.md).

`migrations/data/20260908_audit_resource_timestamps.py` remains a read-only diagnostic for databases that have not reached revision `8c4f2a1d7e60`. Do not replay the completed production procedure against a current environment.
