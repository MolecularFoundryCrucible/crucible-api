-- Phase 1 (expand) of the permission system redesign.
-- See docs/permission_system_plan.md -> Migration path.
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
ALTER TABLE resourceacl
    ADD COLUMN IF NOT EXISTS role resource_role;

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
-- first; role is included so the comparison is answered from the index alone.
CREATE INDEX IF NOT EXISTS ix_resourceacl_group_mfid_role
    ON resourceacl (accessgroup_id, mfid, role);

-- The virtual public group. Membership is injected at auth time and never
-- stored, so this row exists only to give ACL entries something to point at.
INSERT INTO accessgroup (group_name)
SELECT 'public'
WHERE NOT EXISTS (SELECT 1 FROM accessgroup WHERE group_name = 'public');

COMMIT;
