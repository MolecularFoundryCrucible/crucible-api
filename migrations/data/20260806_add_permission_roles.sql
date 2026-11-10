-- Phase 1 (expand) of the permission system redesign.
-- See the migration path in the original plan: git show 0443fdd:docs/permission_system_plan.md
--
-- ADDITIVE ONLY. Nothing is dropped and no behaviour changes: read/write and
-- both public columns stay populated, and the old checks stay in force. The
-- drops happen in phase 5, after the new path is trusted, so that a rollback
-- between here and there is a code revert rather than a restore from backup.
--
-- Run migrations/audit_permission_preconditions.py BEFORE this.
-- Run migrations/backfill_permission_roles.py AFTER this.
-- Staging first, then production.
--
-- The one_owner_per_group index is NOT created here. It belongs at the end of
-- the backfill, once every standing has been assigned.
--
-- WARNING for fresh environments. The SQLModel models declare permission,
-- membership role, and platform_role as str so SQLite-backed tests can create the tables.
-- That means SQLModel.metadata.create_all() against an empty Postgres produces
-- VARCHAR columns, and the ADD COLUMN IF NOT EXISTS statements below then
-- silently do nothing — leaving a database whose columns are the wrong type
-- and whose ::resource_role casts fail. Bootstrapping a new environment must
-- run this file against tables that do not yet have these columns, or drop
-- them first. Existing databases are unaffected: their tables predate the
-- columns entirely.

BEGIN;

-- Declaration order is the ordering. role >= 'editor' and LEAST(a, b) then
-- work natively, with no CASE expression in the list-filter path.
DO $$ BEGIN
    CREATE TYPE resource_role AS ENUM ('viewer', 'contributor', 'editor', 'admin', 'owner');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE platform_role AS ENUM ('none', 'support', 'admin');
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Standing in a group. Nullable for now: the backfill assigns every row, and
-- phase 4 sets NOT NULL once nothing can write a null.
ALTER TABLE useraccessgrouplink
    ADD COLUMN IF NOT EXISTS role resource_role;

-- What a group was granted on a resource. The MIN of this and the standing
-- above is the effective role.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'resourceacl'
          AND column_name = 'role'
    ) THEN
        RAISE EXCEPTION 'resourceacl.role already exists; resolve the intermediate schema before adding permission';
    END IF;
END $$;

ALTER TABLE resourceacl
    ADD COLUMN IF NOT EXISTS permission resource_role;

-- Bypass lives on the principal, never as ACL rows.
ALTER TABLE "user"
    ADD COLUMN IF NOT EXISTS platform_role platform_role NOT NULL DEFAULT 'none';

-- Instruments are not deleted; status tracks lifecycle.
ALTER TABLE instrument
    ADD COLUMN IF NOT EXISTS status VARCHAR NOT NULL DEFAULT 'active';

-- Covering index for the permission check and the list filter. Created in
-- phase 1 rather than after cutover so that shadow mode and the first live
-- traffic are measured against an indexed table.
-- accessgroup_id leads because every query filters on the caller's groups
-- first; permission is included so the comparison is answered from the index alone.
CREATE INDEX IF NOT EXISTS ix_resourceacl_group_mfid_permission
    ON resourceacl (accessgroup_id, mfid, permission);

-- The virtual public group. Membership is injected at auth time and never
-- stored, so this row exists only to give ACL entries something to point at.
INSERT INTO accessgroup (group_name)
SELECT 'public'
WHERE NOT EXISTS (SELECT 1 FROM accessgroup WHERE group_name = 'public');

COMMIT;
