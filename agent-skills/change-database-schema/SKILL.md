---
name: change-database-schema
description: Add or change a database table or column, write the Alembic migration for it, or add a one-time data change. Use for any edit to app/tables.py or anything under migrations/.
---

# Change the database schema

## 1. Model and migration together

1. Edit the table in `app/tables.py`.
2. Write the revision in `migrations/versions/`, named `YYYYMMDD_<revision>_<what>.py`, with a one-line docstring saying what it adds. Autogenerate needs a migrated local database: `DB_CONF_FILE=.env.local uv run alembic revision --autogenerate -m "<what>"`, then review it by hand.
3. Keep one head: `uv run alembic heads` prints a single revision.
4. Write `downgrade`.

## 2. Keep running servers working

A migration runs before the new code and while the old code still serves.

- Add tables and columns; make new columns nullable or give them a server default.
- Do not drop or rename what the previous release reads. Remove it in a later release.
- A new table used by tests goes into `TEST_TABLES` in `tests/conftest.py`.

## 3. Data changes

- A step every upgraded installation must run is a server command in `app/cli/` (the image does not contain `migrations/data/`). It reports by default and writes only with `--commit`.
- A one-time fix for one deployment is a dated script in `migrations/data/`, with the same dry-run default and `--yes-this-is-production` for the production database.
- After loading rows that carry their own ids, run `python -m app.cli db sequences`.

## 4. Check and record

- Run the PostgreSQL-backed tests (skill `run-checks`).
- Add a line under `Unreleased` in `CHANGELOG.md` naming the revision: "Needs migration `<revision>`."
- If operators must do something, add the step to the upgrade guide in `docs/upgrading/`.

## Never

Run a migration or a data write against staging or production. The user runs those; you may verify afterwards with read-only queries when asked.
