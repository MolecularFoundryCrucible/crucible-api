# Crucible API

## Migrations

Every change to a `table=True` model in `app/models/core_models.py` needs a
matching Alembic revision in `migrations/versions/` (schema) — check with
`alembic heads`/`alembic history` after editing models; there should be one
linear head. Pydantic-only Create/Update/Read models need no migration.

One-off backfill/audit scripts go in `migrations/data/`, prefixed
`YYYYMMDD_`, dry-run by default with a `--commit` flag and a production
guard (see existing scripts there for the pattern).

Never run a migration or backfill against a real (staging/production)
database — write the script/file only; the user runs it.
