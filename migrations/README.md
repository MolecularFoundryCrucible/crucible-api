# migrations/

Two different things, kept apart on purpose:

- **`versions/`** — Alembic-managed schema history. Every change to a table's
  shape (`ALTER TABLE`, `CREATE TYPE`, indexes) is a revision here from
  2026-08-10 onward. Generate with `alembic revision --autogenerate -m "..."`,
  review the diff, apply with `alembic upgrade head`. See `env.py` for how it
  connects (through the app's own `get_engine()`, not a URL in `alembic.ini`).

- **`data/`** — one-off scripts: backfills, precondition audits, cleanup.
  Deliberately *not* Alembic revisions — these mutate data with real
  judgment calls (dry-run by default, production guarded, often
  self-verifying), which doesn't fit an upgrade/downgrade model. Files
  predating 2026-08-10 are the pre-Alembic migration history, renamed
  `YYYYMMDD_` by first-commit date; new one-off scripts keep landing here
  the same way.

Full operational detail, including the staging/production schema gap at the
time of the Alembic switch, is in `docs/permission_migration_runbook.md`.
