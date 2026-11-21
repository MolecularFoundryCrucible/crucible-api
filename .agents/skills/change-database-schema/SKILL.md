---
name: change-database-schema
description: Use when adding or changing a database table or column, writing an Alembic migration, or adding a data step that upgraded installations must run - any edit to app/tables.py or under migrations/.
---

# Change the database schema

## 1. Model and migration together

1. Edit the table in `app/tables.py`.
2. Write the revision in `migrations/versions/`, named `YYYYMMDD_<revision>_<what>.py`, with a one-line docstring saying what it does. Autogenerate needs a migrated local database: `DB_CONF_FILE=.env.local uv run alembic revision --autogenerate -m "<what>"`, then review it by hand.
3. Keep one head: `uv run alembic heads` prints a single revision.
4. Write `downgrade`.

## 2. Keep running servers working

A migration runs before the new code and while the old code still serves.

- Add tables and columns; make new columns nullable or give them a server default.
- Do not drop or rename what the previous release reads. Remove it in a later release.
- A new table used by tests goes into `TEST_TABLES` in `tests/conftest.py`.

## 3. Data changes

- A step every upgraded installation must run is a server command in `app/cli/`, with its logic in `app/services/` or `app/auth/`. It reports by default and writes only with `--commit`.
- A one-time fix for one deployment does not go in this repository; it belongs with that deployment's configuration.
- After loading rows that carry their own ids, run `python -m app.cli db sequences`.

## 4. Check and record

- Run the PostgreSQL-backed tests (skill `run-checks`). Test the behavior the change enables, not the migration itself: no test for a single revision or a dropped table.
- Add a line under `Unreleased` in `CHANGELOG.md` naming the revision: "Needs migration `<revision>`."
- If operators must do something, add the step to the upgrade guide in `docs/upgrading/`.

## Never

Run a migration or a data write against a shared deployment (staging, production). Its operators run those; afterwards you may verify with read-only queries when asked.
