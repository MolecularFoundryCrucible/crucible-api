# Database migrations

Use Alembic revisions in `migrations/versions/` for schema changes and reviewed scripts in `migrations/data/` for audits, backfills, and one-time data cleanup.

## New installations

`python -m app.cli db upgrade` brings the configured database to the latest schema:

- An empty database (no tables in `public`) gets `migrations/schema/baseline_<revision>.sql` in one transaction, including the `vector` and `pg_trgm` extensions and the `public` access group, is stamped at that revision, and then gets the migrations after it. If the baseline fails, nothing is left behind; if a later migration fails, the database stays at the last completed revision and running the command again continues from there.
- A database with `alembic_version` gets its pending migrations (`alembic upgrade head`).
- A database with tables but no `alembic_version` is refused.
- Concurrent runs wait for each other (PostgreSQL advisory lock).

`vector` is not a trusted extension, so either the database user is a superuser or an administrator creates both extensions in the database first (`CREATE EXTENSION vector; CREATE EXTENSION pg_trgm;`); the application user then only needs to own the database.

The database named by `ALEMBIC_PRODUCTION_DATABASE` (default `crucible`) is refused unless `--yes-this-is-production` is given, the same guard as `alembic -x yes-this-is-production=true`; set it to an empty value on installations without a separate production database.

Readiness (`/health/ready`) expects the migration heads of the running code when `EXPECTED_DB_REVISIONS` is not set.

### The schema baseline

The old migration chain cannot build a database from nothing, so new installations start from a schema baseline: the schema of staging at a known revision (`BASELINE_REVISION` in `app/cli/db.py`). To check that a fresh installation matches staging:

```bash
DB_SERVER=127.0.0.1:55433 DB_USERNAME=postgres DB_DATABASE=crucible_fresh PGDB_PW=test \
  .venv/bin/python -m app.cli db upgrade

PGPASSWORD=... scripts/compare_schema.sh \
  postgresql://postgres:test@127.0.0.1:55433/crucible_fresh \
  postgresql://crucible-api-user@127.0.0.1:5433/crucible-staging
```

`scripts/compare_schema.sh` dumps both schemas with the PostgreSQL 17 client in read-only sessions and exits 1 on any difference. On 2026-10-26 at `b8f2d6e41c07` (read-only comparisons):

- Fresh installation vs staging: two differences that exist on staging only, an empty legacy `source` schema and an empty comment on the `public` schema (the baseline leaves schema and extension comments out because they require ownership the application user may not have).
- Fresh installation vs production: one difference. `fk_project_project_lead_orcid_user` is `NOT VALID` on production, as revision `1bb842c39928` created it for legacy projects whose lead has no user row; a fresh installation enforces it fully. Staging was validated by hand at some point.

To move the baseline to a newer revision (optional; the migrations after it keep working), dump staging at that revision and regenerate:

```bash
docker run --rm --network host -e PGPASSWORD postgres:17 pg_dump --schema-only --no-owner --no-privileges \
  postgresql://crucible-api-user@127.0.0.1:5433/crucible-staging > staging_schema.sql
.venv/bin/python scripts/make_schema_baseline.py staging_schema.sql <revision>
```

Then update `BASELINE_REVISION`, delete the old baseline file, and run the comparison above.

## Schema changes

Generate and review a revision before applying it:

```bash
DB_CONF_FILE=.env.local .venv/bin/alembic revision --autogenerate -m "describe the change"
DB_CONF_FILE=.env.local .venv/bin/alembic upgrade head
```

The file named by `DB_CONF_FILE` must provide the database password as `PGDB_PW` or `PGDB_PW_VERSION` (a full `projects/<project>/secrets/<secret>/versions/<n>` path read with your Application Default Credentials); there is no built-in Secret Manager default. Data scripts that import API route modules also need `CRUCIBLE_SECRET_KEY` the same way.

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

## Platform contributor role migration

Revision `7a1c4e8d2f90` adds `contributor` to the PostgreSQL `platform_role` enum. It does not update user rows and can run while the API remains available.

Staging and production completed this migration on 2026-09-16. The production migration ran online through the Cloud SQL proxy, and the main API completed a zero-downtime rolling deployment.

```bash
DB_CONF_FILE=.env.prod \
.venv/bin/alembic -x yes-this-is-production=true current

DB_CONF_FILE=.env.prod \
.venv/bin/alembic -x yes-this-is-production=true upgrade 7a1c4e8d2f90

DB_CONF_FILE=.env.prod \
.venv/bin/alembic -x yes-this-is-production=true current
```

Both environments are at revision `7a1c4e8d2f90`, and their enum contains `none`, `contributor`, `support`, and `admin`. Existing service accounts remain unchanged until a human platform administrator updates them individually.
