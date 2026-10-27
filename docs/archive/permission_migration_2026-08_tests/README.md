# Archived: tests of the August 2026 permission migration

Archived 2026-10-27. Not run: the file names do not start with `test_`, so pytest does not collect them.

`archived_test_migration_permission_roles.py` ran the one-off permission migration (`migrations/data/20260806_add_permission_roles.sql` and `migrations/data/20260806_backfill_permission_roles.py`) against a throwaway PostgreSQL database and checked the resulting roles. `archived_pg_fixture.py` is the fixture it used: it rebuilt the pre-migration schema from the current models by dropping the columns the migration added, then seeded pre-migration data.

Why archived:

- The migration is complete on staging and production, and its [runbook](../permission_migration_runbook_2026-08.md) says not to replay it.
- New installations start from the schema baseline (`migrations/schema/`), which already contains its result.
- The fixture could no longer rebuild the old schema: later revisions made columns such as `resourceacl.permission` required, so seeding pre-migration rows fails.

The permission checks themselves stay tested against PostgreSQL in `tests/test_require_permission.py`, on the current schema.
