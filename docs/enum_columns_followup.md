# PostgreSQL enum columns declared as strings: follow-up

Status: found 2026-10-27, not scheduled. No current code path is affected.

## Problem

Several columns are PostgreSQL enum types in the database but plain `str` in the models, so the SQLite-backed tests can create the tables. When SQLAlchemy inserts several rows of a table with a database-generated id in one flush, it uses a multi-row `INSERT ... SELECT p0::VARCHAR, ...` with each value cast to the model's column type. PostgreSQL then refuses the enum columns:

```
column "group_type" is of type access_group_type but expression is of type character varying
```

Reproduced on PostgreSQL 17 with the schema baseline: adding two `AccessGroup` rows in one flush fails; so does adding two `User` rows (`platform_role`). Single-row inserts work, and tables whose primary key is not generated (`useraccessgrouplink.role`, `resourceacl.permission`) are inserted without casts and work.

Today the application creates users and access groups one at a time, so nothing fails in production. A future change that creates several in one flush (bulk invitations, an import, a project with several groups) would fail on PostgreSQL only, which the SQLite tests would not catch. `tests/pg_fixture.py` inserts them one at a time for this reason.

## Proposal

1. Audit every model column that is an enum in `migrations/schema/baseline_*.sql` (`resource_role`, `platform_role`, `access_group_type`, and any others) against its model declaration.
2. Declare those columns with their enum type (`sa_column=Column(Enum(..., name="access_group_type", create_type=False))`, or a `TypeDecorator` that is a string on SQLite and the named enum on PostgreSQL), so casts use the right type.
3. Add a PostgreSQL test that inserts two rows of each affected model in one flush.
