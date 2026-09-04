# Future work: rename the `idtype` table to `resource`

Not started. Documented here so the scope doesn't need re-deriving later.

## Why

The SQLModel class is already `Resource` (`app/models/core_models.py`), but the physical Postgres table is still `idtype` — a naming holdover the class name never matched. Same shape of naming-clarity fix as the `ResourceACL.role` → `permission` rename: the code already calls it what it is, the schema hasn't caught up.

## Scope, verified by direct search — no route or utility code is affected

Everything goes through the `Resource` ORM class (`select(Resource).where(...)`), which SQLAlchemy maps to whichever table `__tablename__` names, transparently. Confirmed via grep: zero raw SQL or string literals referencing `idtype` anywhere in `app/api/routes/` or `app/utils/`.

The only things that actually need to change:

**`app/models/core_models.py` — 4 lines:**
- `Resource.__tablename__ = "idtype"` → `"resource"`
- `ResourceACL.mfid`'s `foreign_key="idtype.mfid"` → `"resource.mfid"`
- `ScientificMetadataBinary.unique_id`'s `foreign_key="idtype.mfid"` → `"resource.mfid"`
- `DeletionRequest.resource_id`'s `foreign_key="idtype.mfid"` → `"resource.mfid"`

These three `foreign_key=` strings are SQLModel metadata, not something SQLAlchemy infers from the `Resource` class name — if left pointing at `idtype`, a fresh environment built via `SQLModel.metadata.create_all()` (the test suite, or bootstrapping a new deployment) would try to create FK constraints against a table that no longer exists.

**One new Alembic migration:**
```sql
ALTER TABLE idtype RENAME TO resource;
```
Metadata-only on Postgres (not a table rewrite), same cheap shape as the `role`→`permission` rename. Existing FK constraints on `resourceacl`, `scientific_metadata_new`, and `deletionrequest` are tracked by Postgres internally (not by name), so they keep working across the rename without needing to be dropped/recreated.

**Docs to update for accuracy** (prose/SQL examples mentioning `idtype`):
- `docs/archive/permission_migration_runbook_2026-08.md`
- `docs/archive/schema_hygiene_pre_permission_migration.md`
- `docs/remote_file_records.md`
- `docs/permission_system_plan.md`

**Tests to update:**
- `tests/test_project_rename.py` — one docstring mention
- `tests/test_migration_permission_roles.py` — one raw SQL query (`SELECT resource_type FROM idtype WHERE mfid = ...`)

## What NOT to touch

Historical migration files that reference `idtype` in raw SQL are a correct record of what ran against a table that *was* named that at the time — do not edit them retroactively:
- `migrations/versions/20260810_8ca4a71f1bed_add_accessgroup_owner_orcid_and_.py`
- `migrations/versions/20260810_4ae6482731ed_add_project_unique_id.py`
- `migrations/versions/20260813_bb780697b63b_drop_accessgroup_owner_orcid_and_.py`
- `migrations/data/20260813_repoint_project_resource_identity.py`
- `migrations/data/20260806_audit_permission_preconditions.py`
- `migrations/data/20260806_backfill_permission_roles.py`
- `migrations/data/20260807_delete_dangling_project_samples.py`
- `migrations/data/20260504_add_public_to_idtype.sql`
- `migrations/data/20260507_fix_scientific_metadata_new_fk.sql`

## Deploy ordering

Same as the `role`→`permission` rename: this is a hard cutover, not expand/contract. Schema migration and code deploy together, back to back — old code reading table `idtype` and new code reading table `resource` cannot both work against the same schema.
