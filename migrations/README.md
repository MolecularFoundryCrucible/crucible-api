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

Revision `8c4f2a1d7e60` converts the API-owned `idtype.creation_time` and `idtype.modification_time` fields from timezone-qualified ISO strings to nullable PostgreSQL `TIMESTAMPTZ` columns with database defaults. New resources receive UTC values from the API and omitted values receive the database default, while unknown legacy values remain null. Before applying it, run the read-only audit:

```bash
DB_CONF_FILE=.env.prod \
.venv/bin/python migrations/data/20260908_audit_resource_timestamps.py
```

The audit reports null, timezone-less, invalid, non-UTC, and UTC values separately for both columns. Any unparseable non-null timestamp blocks migration. Missing legacy timestamps remain null rather than receiving invented history. Timezone-less legacy values are interpreted in the `America/Los_Angeles` zone, applying the applicable Pacific standard or daylight-saving offset for their date. The migration repairs a missing modification time from the corresponding creation time when one exists and otherwise leaves it null. Legacy timestamp strings with explicit offsets are preserved as instants even when they use PostgreSQL's space separator or shortened UTC offset. The producer-supplied `dataset.timestamp` field is not included in this policy.

The audit script does not mutate data. Pause old API replicas and writers before the schema migration, apply the revision, deploy the API version that models these fields as timezone-aware datetimes and serializes them as canonical UTC RFC 3339 values, and only then restore traffic. Do not run old and new timestamp models concurrently.
