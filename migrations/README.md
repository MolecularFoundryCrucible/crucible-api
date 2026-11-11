# Database migrations

Schema changes are Alembic revisions in `migrations/versions/`. Commands read the database settings from the environment, or from the file named by `DB_CONF_FILE`.

## Applying migrations

```bash
python -m app.cli db upgrade
```

- An empty database receives the schema baseline, then every later revision.
- A database already managed by Alembic receives its pending revisions.
- A database with tables but no `alembic_version` table is refused.
- Concurrent runs are serialized.
- A revision waits at most `ALEMBIC_LOCK_TIMEOUT` (5 s) for a table lock; if it times out, run the command again.

A new database requires the `vector` and `pg_trgm` extensions. Either the database user is a superuser, or an administrator creates both extensions beforehand.

## Production databases

The database named by `ALEMBIC_PRODUCTION_DATABASE` (default `crucible`) is refused without an explicit flag, including for read-only commands:

```bash
DB_CONF_FILE=.env.prod python -m app.cli db upgrade --yes-this-is-production
DB_CONF_FILE=.env.prod alembic -x yes-this-is-production=true current
```

Before a production migration:

1. Confirm which database the settings point to.
2. Take a backup.
3. Read the upgrade guide for the release in `docs/upgrading/`; some releases require steps before or after the migration.

## Schema changes

1. Edit the table in `app/tables.py`.
2. Generate a revision against a migrated local database, then review it manually:

   ```bash
   DB_CONF_FILE=.env.local uv run alembic revision --autogenerate -m "describe the change"
   DB_CONF_FILE=.env.local uv run alembic upgrade head
   ```

3. Confirm there is a single head: `uv run alembic heads`.

Add tables and columns; do not drop or rename anything the previous release still reads. The migration runs while the previous version is still serving requests.

## Data changes

A data change that every upgraded installation must run is a server command (`python -m app.cli ...`), shipped in the image. It reports the planned changes by default and writes only with `--commit`. Examples: `identity backfill` and `owners backfill`, both run once after upgrading from 3.x.

A one-time correction for a single deployment does not belong in this repository; keep it with that deployment's configuration.

## Sequences after partial data loads

Copying rows from another database together with their `id` values (a subset of production into staging, a partial restore) does not advance the id sequences. The next insert then fails with a duplicate key error, which the API reports as a 500. A full dump and restore is not affected.

```bash
python -m app.cli db sequences            # lists counters behind their tables; exit 1 if any
python -m app.cli db sequences --commit   # moves them up
```

## Schema baseline

A new database is created from `migrations/schema/baseline_<revision>.sql`, the schema at a known revision (`BASELINE_REVISION` in `app/cli/db.py`), because the oldest revisions cannot build a database from an empty state.

To move the baseline to a newer revision (optional):

```bash
pg_dump --schema-only --no-owner --no-privileges <database at that revision> > schema.sql
uv run python scripts/make_schema_baseline.py schema.sql <revision>
```

Then update `BASELINE_REVISION`, delete the old baseline file, and compare a fresh database with the source: `scripts/compare_schema.sh <fresh database url> <source database url>` exits 1 on any difference.

## Connecting to Cloud SQL

Run the Cloud SQL Auth Proxy on a free local port and point an untracked environment file at it. Confirm the database name before any write.

```bash
cloud-sql-proxy --address 127.0.0.1 --port 5433 <project>:<region>:<instance>
```
