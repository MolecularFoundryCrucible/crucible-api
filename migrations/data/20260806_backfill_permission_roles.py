"""
Phase 1 backfill for the permission system redesign.
See docs/permission_system_plan.md -> Migration path.

Run AFTER migrations/data/20260806_add_permission_roles.sql, and after
migrations/data/20260806_audit_permission_preconditions.py reports no blockers.

Dry run by default — prints what every step would do and rolls back. Pass
--commit to apply. Staging first, then production.

Every statement computes its final value directly rather than layering updates,
so re-running is safe and converges to the same state.

Usage (from inside the pod):
    /root/.venv/bin/python migrations/data/20260806_backfill_permission_roles.py
    /root/.venv/bin/python migrations/data/20260806_backfill_permission_roles.py --commit
"""
import argparse
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine

# Group names that are not project, ORCID or instrument groups.
SPECIAL_GROUPS = ("admin", "read_only", "public")

# A group is classified by what its name matches. Reused by several steps, so
# it is defined once as a CTE fragment rather than repeated.
GROUP_KINDS = """
    group_kind AS (
        SELECT ag.id,
               ag.group_name,
               CASE
                   WHEN ag.group_name IN ('admin', 'read_only', 'public') THEN 'special'
                   WHEN EXISTS (SELECT 1 FROM "user" u WHERE u.orcid = ag.group_name) THEN 'orcid'
                   WHEN EXISTS (SELECT 1 FROM project p WHERE p.project_id = ag.group_name) THEN 'project'
                   WHEN EXISTS (SELECT 1 FROM instrument i WHERE lower(i.instrument_name) = ag.group_name) THEN 'instrument'
                   ELSE 'orphan'
               END AS kind
        FROM accessgroup ag
    )
"""


def step(session, title, sql, expect_rows=True):
    result = session.execute(text(sql))
    n = result.rowcount if expect_rows else 0
    print(f"  {title:<58} {n if n >= 0 else '?'}")
    return n


def report_group_kinds(session):
    print("\n[0] group classification")
    rows = session.execute(text(f"WITH {GROUP_KINDS} "
                                "SELECT kind, count(*) FROM group_kind GROUP BY 1 ORDER BY 2 DESC")).all()
    for kind, n in rows:
        print(f"  {kind:<58} {n}")

    orphans = session.execute(text(f"""
        WITH {GROUP_KINDS}
        SELECT group_name FROM group_kind WHERE kind = 'orphan' ORDER BY group_name LIMIT 20
    """)).all()
    if orphans:
        print("  orphan groups (match no user, project or instrument) — likely")
        print("  the typo-created groups from unvalidated instrument_name:")
        for (name,) in orphans:
            print(f"      {name}")


def backfill_standings(session):
    """Assign a standing to every useraccessgrouplink row.

    Members become `editor`, not `contributor`: the project group holds write on
    every project resource today, so anything lower silently strips access.
    Special and unclassified groups fall to `viewer`.

    One CASE rather than layered updates, so re-running converges.
    """
    print("\n[1] standings on useraccessgrouplink")
    return step(session, "rows assigned a standing", f"""
        WITH {GROUP_KINDS},
        target AS (
            SELECT l.user_id,
                   l.accessgroup_id,
                   (CASE
                        WHEN gk.kind = 'orcid' THEN 'owner'
                        WHEN gk.kind = 'project' AND EXISTS (
                            SELECT 1 FROM project p
                            JOIN "user" u ON u.orcid = p.project_lead_orcid
                            WHERE p.project_id = gk.group_name AND u.id = l.user_id
                        ) THEN 'owner'
                        WHEN gk.kind = 'project' THEN 'editor'
                        WHEN gk.kind = 'instrument' THEN 'admin'
                        ELSE 'viewer'
                    END)::resource_role AS role
            FROM useraccessgrouplink l
            JOIN group_kind gk ON gk.id = l.accessgroup_id
        )
        UPDATE useraccessgrouplink l
        SET role = t.role
        FROM target t
        WHERE t.user_id = l.user_id
          AND t.accessgroup_id = l.accessgroup_id
          AND l.role IS DISTINCT FROM t.role
    """)


def hijacked_project_mfids(session):
    """Projects whose project_id already names a resource of another type.

    Granting the project's group `owner` on that mfid would hand it someone
    else's resource.
    """
    return session.execute(text("""
        SELECT r.mfid, r.resource_type FROM idtype r
        JOIN project p ON p.project_id = r.mfid
        WHERE r.resource_type <> 'project'
        ORDER BY 1
    """)).all()


def create_project_resources(session):
    """Give each project an idtype row and one ACL entry — its own group at
    `owner` — so effective role on a project equals standing in it."""
    print("\n[2] projects as resources")

    hijacked = hijacked_project_mfids(session)
    if hijacked:
        print(f"  {len(hijacked)} project_id(s) collide with an existing resource:")
        for mfid, rtype in hijacked:
            print(f"      project_id={mfid!r} is already a {rtype!r} resource — SKIPPED")
        print("      granting the project's group owner on that mfid would hand it")
        print("      someone else's resource. Clean these up before committing.")

    created = step(session, "idtype rows created for projects", """
        INSERT INTO idtype (mfid, resource_type, public)
        SELECT p.project_id, 'project', FALSE
        FROM project p
        WHERE NOT EXISTS (SELECT 1 FROM idtype r WHERE r.mfid = p.project_id)
    """)

    acl = step(session, "project ACL entries (own group at owner)", """
        INSERT INTO resourceacl (mfid, accessgroup_id, read, write, role)
        SELECT p.project_id, ag.id, TRUE, TRUE, 'owner'::resource_role
        FROM project p
        JOIN accessgroup ag ON ag.group_name = p.project_id
        JOIN idtype r ON r.mfid = p.project_id AND r.resource_type = 'project'
        WHERE NOT EXISTS (
            SELECT 1 FROM resourceacl a
            WHERE a.mfid = p.project_id AND a.accessgroup_id = ag.id
        )
    """)
    return created, acl


def drop_bypass_acl_rows(session):
    """Delete the admin and read_only ACL rows.

    Requires the admin-group bypass in auth_utils to be deployed first, or
    admin-group members lose everything.
    """
    print("\n[3] remove materialised bypass rows")

    admin_rows = step(session, "admin-group ACL rows deleted", """
        DELETE FROM resourceacl a USING accessgroup ag
        WHERE ag.id = a.accessgroup_id AND ag.group_name = 'admin'
    """)
    ro_rows = step(session, "read_only ACL rows deleted", """
        DELETE FROM resourceacl a USING accessgroup ag
        WHERE ag.id = a.accessgroup_id AND ag.group_name = 'read_only'
    """)
    return admin_rows, ro_rows


def backfill_acl_grants(session):
    """Assign a grant to every resourceacl row.

    The grant is a ceiling, not an assignment: project `admin`, instrument
    `contributor`, ORCID `owner`. Computed once in a CTE so the row count
    reports only real changes on a re-run.
    """
    print("\n[4] grants on resourceacl")
    return step(session, "rows assigned a grant", f"""
        WITH {GROUP_KINDS},
        target AS (
            SELECT gk.id AS accessgroup_id,
                   (CASE
                        WHEN gk.kind = 'orcid' THEN 'owner'
                        WHEN gk.kind = 'project' THEN 'admin'
                        WHEN gk.kind = 'instrument' THEN 'contributor'
                        ELSE 'viewer'
                    END)::resource_role AS role
            FROM group_kind gk
        )
        UPDATE resourceacl a
        SET role = t.role
        FROM target t
        JOIN group_kind gk2 ON gk2.id = t.accessgroup_id
        WHERE t.accessgroup_id = a.accessgroup_id
          AND a.role IS DISTINCT FROM t.role
          -- Never touch a project's entry on itself. Step 2 sets that to
          -- `owner` so effective role on a project equals standing in it; the
          -- generic project grant of `admin` applies to a project's grant on
          -- OTHER resources, and would otherwise demote every lead to admin
          -- on their own project and remove their ability to transfer it.
          AND NOT (gk2.kind = 'project' AND a.mfid = gk2.group_name)
    """)


def migrate_public_flags(session):
    """Replace both public booleans with an ACL entry against the public group.

    Dataset.public is authoritative for datasets, Resource.public for samples.
    Both columns stay until phase 5.
    """
    print("\n[5] public flags become ACL entries")

    datasets = step(session, "public datasets", """
        INSERT INTO resourceacl (mfid, accessgroup_id, read, write, role)
        SELECT d.unique_id, ag.id, TRUE, FALSE, 'viewer'::resource_role
        FROM dataset d
        CROSS JOIN accessgroup ag
        WHERE ag.group_name = 'public' AND d.public = TRUE
          AND NOT EXISTS (
            SELECT 1 FROM resourceacl a
            WHERE a.mfid = d.unique_id AND a.accessgroup_id = ag.id
        )
    """)

    others = step(session, "other public resources (samples etc.)", """
        INSERT INTO resourceacl (mfid, accessgroup_id, read, write, role)
        SELECT r.mfid, ag.id, TRUE, FALSE, 'viewer'::resource_role
        FROM idtype r
        CROSS JOIN accessgroup ag
        WHERE ag.group_name = 'public' AND r.public = TRUE
          AND r.resource_type <> 'dataset'
          AND NOT EXISTS (
            SELECT 1 FROM resourceacl a
            WHERE a.mfid = r.mfid AND a.accessgroup_id = ag.id
        )
    """)
    return datasets, others


def set_platform_admins(session):
    """Promote every admin-group member to platform_role='admin'.

    Service accounts included, deliberately and temporarily: withholding it
    means they hold nothing at cutover and every instrument stops writing.
    Narrowing them is tracked separately — see "Regressing service account
    rights" in the plan.
    """
    print("\n[6] platform_role")

    promoted = step(session, "admin-group members promoted", """
        UPDATE "user" u
        SET platform_role = 'admin'
        FROM useraccessgrouplink l
        JOIN accessgroup ag ON ag.id = l.accessgroup_id
        WHERE l.user_id = u.id AND ag.group_name = 'admin'
          AND u.platform_role <> 'admin'
    """)

    service_accounts = session.execute(text("""
        SELECT u.username, u.orcid FROM "user" u
        WHERE u.platform_role = 'admin' AND u.is_service_account = true
        ORDER BY u.username
    """)).all()
    humans = session.execute(text("""
        SELECT u.username, u.orcid FROM "user" u
        WHERE u.platform_role = 'admin' AND u.is_service_account IS NOT TRUE
        ORDER BY u.username
    """)).all()

    print(f"  humans with platform_role=admin                            {len(humans)}")
    for username, orcid in humans:
        print(f"      {username or orcid}")

    if service_accounts:
        print(f"  service accounts with platform_role=admin                  {len(service_accounts)}")
        for username, orcid in service_accounts:
            print(f"      {username or orcid}")
        print("      TEMPORARY. These hold a full platform bypass so that cutover")
        print("      does not break ingestion. Narrowing them is tracked work:")
        print("      bind each to its instruments, confirm ingestion, then drop")
        print("      platform_role back to 'none'. Until then any compromised")
        print("      instrument credential reaches every resource.")

    if not humans:
        print("  NOTE: no human holds platform_role=admin — there is no")
        print("        human break-glass path. Worth naming someone.")

    return promoted


def scope_instrument_groups(session):
    """Leave only the service account in each instrument group.

    The one step that removes access people have today.
    """
    print("\n[7] instrument groups become service-account only")

    at_risk = session.execute(text(f"""
        WITH {GROUP_KINDS}
        SELECT ag.group_name, u.orcid, u.username, u.platform_role
        FROM useraccessgrouplink l
        JOIN accessgroup ag ON ag.id = l.accessgroup_id
        JOIN group_kind gk ON gk.id = ag.id
        JOIN "user" u ON u.id = l.user_id
        WHERE gk.kind = 'instrument' AND u.is_service_account IS NOT TRUE
        ORDER BY ag.group_name
    """)).all()

    if at_risk:
        print(f"  {len(at_risk)} human members would be removed:")
        for group_name, orcid, username, prole in at_risk:
            flag = "" if prole == "admin" else "   <- NOT a platform admin"
            print(f"      {group_name:<28} {username or orcid}{flag}")
    else:
        print("  no human members in any instrument group")

    removed = step(session, "human members removed", f"""
        WITH {GROUP_KINDS}
        DELETE FROM useraccessgrouplink l
        USING group_kind gk, "user" u
        WHERE gk.id = l.accessgroup_id AND u.id = l.user_id
          AND gk.kind = 'instrument' AND u.is_service_account IS NOT TRUE
    """)

    created = step(session, "groups created for instruments having none", """
        INSERT INTO accessgroup (group_name)
        SELECT DISTINCT lower(i.instrument_name)
        FROM instrument i
        WHERE NOT EXISTS (
            SELECT 1 FROM accessgroup ag WHERE ag.group_name = lower(i.instrument_name)
        )
    """)
    return removed, created


def verify(session):
    """Check no row is left without a role and no group has two owners."""
    print("\n[8] verification")

    null_standings = session.execute(text(
        "SELECT count(*) FROM useraccessgrouplink WHERE role IS NULL")).scalar()
    null_grants = session.execute(text(
        "SELECT count(*) FROM resourceacl WHERE role IS NULL")).scalar()
    print(f"  useraccessgrouplink rows still null                        {null_standings}")
    print(f"  resourceacl rows still null                                {null_grants}")

    hijacked = hijacked_project_mfids(session)
    print(f"  project_ids colliding with another resource type            {len(hijacked)}")

    dupes = session.execute(text("""
        SELECT ag.group_name, count(*)
        FROM useraccessgrouplink l
        JOIN accessgroup ag ON ag.id = l.accessgroup_id
        WHERE l.role = 'owner'
        GROUP BY ag.group_name HAVING count(*) > 1
        ORDER BY 2 DESC LIMIT 20
    """)).all()
    print(f"  groups with more than one owner                            {len(dupes)}")
    for name, n in dupes:
        print(f"      {name:<40} {n}")

    return null_standings, null_grants, list(dupes) + list(hijacked)


def create_owner_index(session):
    """Build one_owner_per_group. A failure here is pre-existing duplicate-owner
    data, not a migration bug."""
    print("\n[9] one_owner_per_group")
    session.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS one_owner_per_group
            ON useraccessgrouplink (accessgroup_id) WHERE role = 'owner'
    """))
    print("  index created")


PRODUCTION_DB = "crucible"


def confirm_target(session, allow_production: bool) -> str:
    """Ask the server which database this is, and refuse production unless told.

    Prod and staging share a Cloud SQL instance, host and user — only the
    database name differs, so DB_CONF_FILE alone is not enough of a guard.
    """
    actual = session.execute(text("SELECT current_database()")).scalar()
    user = session.execute(text("SELECT current_user")).scalar()
    print(f"connected to  : {actual!r} as {user!r}")

    if actual == PRODUCTION_DB and not allow_production:
        print(f"\nREFUSING TO RUN: {actual!r} is production.")
        print("Re-run with --yes-this-is-production if that is genuinely intended.")
        raise SystemExit(2)
    return actual


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="apply the backfill; without it everything is rolled back")
    parser.add_argument("--skip-index", action="store_true",
                        help="skip one_owner_per_group (use if co-PIs are permitted)")
    parser.add_argument("--yes-this-is-production", action="store_true",
                        help=f"required to run against the {PRODUCTION_DB!r} database")
    args = parser.parse_args()

    engine = get_engine()
    mode = "COMMIT" if args.commit else "DRY RUN (rolled back)"
    print(f"permission role backfill — {engine.url.database} on {engine.url.host}")
    print(f"mode: {mode}")

    with Session(engine) as session:
        confirm_target(session, args.yes_this_is_production)
        report_group_kinds(session)
        backfill_standings(session)
        create_project_resources(session)
        drop_bypass_acl_rows(session)
        backfill_acl_grants(session)
        migrate_public_flags(session)
        set_platform_admins(session)
        scope_instrument_groups(session)
        null_standings, null_grants, dupes = verify(session)

        blocked = bool(null_standings or null_grants or dupes)
        if blocked:
            print("\nNOT creating one_owner_per_group — resolve the above first")
        elif args.skip_index:
            print("\n[9] one_owner_per_group skipped (--skip-index)")
        else:
            create_owner_index(session)

        if args.commit and not blocked:
            session.commit()
            print("\nCOMMITTED")
        elif args.commit and blocked:
            session.rollback()
            print("\nROLLED BACK — verification failed, nothing was written")
            return 1
        else:
            session.rollback()
            print("\nrolled back (dry run) — re-run with --commit to apply")

    return 0


if __name__ == "__main__":
    sys.exit(main())
