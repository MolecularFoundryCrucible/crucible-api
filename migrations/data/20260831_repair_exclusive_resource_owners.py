"""Repair legacy identity ACLs for exclusive resource ownership.

The canonical stored owner keeps or receives the owner grant. Other identity
groups that were promoted to owner by the initial backfill return to editor for
legacy write access or viewer for legacy read-only access. A registered
canonical owner receives a missing owner ACL. Unresolved owner references remain
ownerless.

The dry run is read-only. The commit uses one set-based update.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260831_repair_exclusive_resource_owners.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260831_repair_exclusive_resource_owners.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"
RESOURCE_OWNERS = """
    SELECT
        dataset.unique_id AS mfid,
        COALESCE(owner_user.orcid, NULLIF(dataset.owner_orcid, '')) AS owner_group_name,
        'identity' AS owner_group_type,
        'dataset' AS resource_type,
        EXISTS (
            SELECT 1 FROM idtype resource
            WHERE resource.mfid = dataset.unique_id
              AND resource.resource_type = 'dataset'
        ) AS resource_exists
    FROM dataset
    LEFT JOIN "user" owner_user ON owner_user.id = dataset.owner_user_id

    UNION ALL

    SELECT
        sample.unique_id AS mfid,
        COALESCE(owner_user.orcid, NULLIF(sample.owner_orcid, '')) AS owner_group_name,
        'identity' AS owner_group_type,
        'sample' AS resource_type,
        EXISTS (
            SELECT 1 FROM idtype resource
            WHERE resource.mfid = sample.unique_id
              AND resource.resource_type = 'sample'
        ) AS resource_exists
    FROM sample
    LEFT JOIN "user" owner_user ON owner_user.id = sample.owner_user_id

    UNION ALL

    SELECT
        instrument.unique_id AS mfid,
        NULLIF(instrument.owner, '') AS owner_group_name,
        'identity' AS owner_group_type,
        'instrument' AS resource_type,
        EXISTS (
            SELECT 1 FROM idtype resource
            WHERE resource.mfid = instrument.unique_id
              AND resource.resource_type = 'instrument'
        ) AS resource_exists
    FROM instrument

    UNION ALL

    SELECT
        resource.mfid,
        project.unique_id AS owner_group_name,
        'project' AS owner_group_type,
        'project' AS resource_type,
        TRUE AS resource_exists
    FROM project
    JOIN idtype resource
      ON resource.resource_type = 'project'
     AND resource.mfid IN (project.project_id, project.unique_id)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="apply changes; otherwise audit only")
    parser.add_argument(
        "--yes-this-is-production",
        action="store_true",
        help=f"required to write to the {PRODUCTION_DB!r} database",
    )
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()"),).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (read only)'}")

        if database == PRODUCTION_DB and args.commit and not args.yes_this_is_production:
            print(f"REFUSING TO RUN: {database!r} is production.")
            return 2

        counts = session.execute(text(f"""
            WITH resource_owners AS (
                {RESOURCE_OWNERS}
            ), state AS (
                SELECT
                    acl.mfid,
                    acl.permission::text AS permission,
                    acl.read,
                    acl.write,
                    access_group.group_name,
                    access_group.group_type::text AS group_type,
                    resource_owners.owner_group_name,
                    resource_owners.owner_group_type,
                    CASE
                        WHEN access_group.group_type::text = 'identity'
                         AND access_group.group_name = resource_owners.owner_group_name
                            THEN 'owner'
                        WHEN access_group.group_type::text = 'identity'
                         AND acl.permission = 'owner'
                         AND acl.write
                            THEN 'editor'
                        WHEN access_group.group_type::text = 'identity'
                         AND acl.permission = 'owner'
                            THEN 'viewer'
                        ELSE acl.permission::text
                    END AS projected_permission
                FROM resourceacl acl
                JOIN accessgroup access_group ON access_group.id = acl.accessgroup_id
                LEFT JOIN resource_owners ON resource_owners.mfid = acl.mfid
            )
            SELECT
                count(*) FILTER (
                    WHERE permission = 'owner'
                      AND group_type = 'identity'
                      AND group_name IS DISTINCT FROM owner_group_name
                      AND write
                ) AS demote_to_editor,
                count(*) FILTER (
                    WHERE permission = 'owner'
                      AND group_type = 'identity'
                      AND group_name IS DISTINCT FROM owner_group_name
                      AND NOT write
                ) AS demote_to_viewer,
                count(*) FILTER (
                    WHERE permission <> 'owner'
                      AND group_type = 'identity'
                      AND group_name = owner_group_name
                ) AS promote_to_owner,
                count(*) FILTER (
                    WHERE permission = 'owner'
                      AND group_type <> 'identity'
                      AND group_name IS DISTINCT FROM owner_group_name
                ) AS unexpected_nonidentity_owners,
                (
                    SELECT count(*)
                    FROM (
                        SELECT mfid
                        FROM state
                        WHERE projected_permission = 'owner'
                        GROUP BY mfid
                        HAVING count(*) > 1
                    ) duplicate_resources
                ) AS projected_duplicate_resources,
                (
                    SELECT count(*)
                    FROM resource_owners owner_row
                    WHERE owner_row.owner_group_name IS NOT NULL
                      AND owner_row.owner_group_type = 'identity'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM state
                          WHERE state.mfid = owner_row.mfid
                            AND state.group_type = 'identity'
                            AND state.group_name = owner_row.owner_group_name
                      )
                ) AS missing_canonical_owner_acls,
                (
                    SELECT count(*)
                    FROM resource_owners owner_row
                    JOIN accessgroup owner_group
                      ON owner_group.group_name = owner_row.owner_group_name
                     AND owner_group.group_type::text = 'identity'
                    WHERE owner_row.owner_group_type = 'identity'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM state
                          WHERE state.mfid = owner_row.mfid
                            AND state.group_type = 'identity'
                            AND state.group_name = owner_row.owner_group_name
                      )
                ) AS missing_registered_owner_acls,
                (
                    SELECT count(*)
                    FROM resource_owners owner_row
                    JOIN accessgroup owner_group
                      ON owner_group.group_name = owner_row.owner_group_name
                     AND owner_group.group_type::text = 'identity'
                    WHERE owner_row.owner_group_type = 'identity'
                      AND owner_row.resource_exists
                      AND NOT EXISTS (
                          SELECT 1
                          FROM state
                          WHERE state.mfid = owner_row.mfid
                            AND state.group_type = 'identity'
                            AND state.group_name = owner_row.owner_group_name
                      )
                ) AS insertable_owner_acls
            FROM state
        """)).one()

        print(f"demote to editor       : {counts.demote_to_editor}")
        print(f"demote to viewer       : {counts.demote_to_viewer}")
        print(f"promote to owner       : {counts.promote_to_owner}")
        print(f"unexpected other owners: {counts.unexpected_nonidentity_owners}")
        print(f"duplicates after repair: {counts.projected_duplicate_resources}")
        print(f"missing owner ACLs      : {counts.missing_canonical_owner_acls}")
        print(f"  registered owners     : {counts.missing_registered_owner_acls}")
        print(f"    valid resources     : {counts.insertable_owner_acls} (create ACL)")
        print(
            f"    missing idtype      : "
            f"{counts.missing_registered_owner_acls - counts.insertable_owner_acls} "
            "(left ownerless)"
        )
        print(
            f"  unresolved references : "
            f"{counts.missing_canonical_owner_acls - counts.missing_registered_owner_acls} "
            "(left ownerless)"
        )

        if counts.unexpected_nonidentity_owners or counts.projected_duplicate_resources:
            print("REFUSING TO RUN: projected ownership state is not exclusive.")
            return 2

        expected_updates = (
            counts.demote_to_editor
            + counts.demote_to_viewer
            + counts.promote_to_owner
        )
        if not args.commit:
            print(f"rows to update          : {expected_updates}")
            print(f"rows to insert          : {counts.insertable_owner_acls}")
            print("dry run only: nothing written")
            return 0

        updated = session.execute(text(f"""
            WITH resource_owners AS (
                {RESOURCE_OWNERS}
            ), targets AS (
                SELECT
                    acl.mfid,
                    acl.accessgroup_id,
                    CASE
                        WHEN access_group.group_name = resource_owners.owner_group_name
                            THEN 'owner'
                        WHEN acl.write
                            THEN 'editor'
                        ELSE 'viewer'
                    END AS permission
                FROM resourceacl acl
                JOIN accessgroup access_group ON access_group.id = acl.accessgroup_id
                LEFT JOIN resource_owners ON resource_owners.mfid = acl.mfid
                WHERE access_group.group_type::text = 'identity'
                  AND (
                      (
                          acl.permission = 'owner'
                          AND access_group.group_name IS DISTINCT FROM resource_owners.owner_group_name
                      )
                      OR (
                          acl.permission <> 'owner'
                          AND access_group.group_name = resource_owners.owner_group_name
                      )
                  )
            )
            UPDATE resourceacl acl
            SET permission = CAST(targets.permission AS resource_role)
            FROM targets
            WHERE targets.mfid = acl.mfid
              AND targets.accessgroup_id = acl.accessgroup_id
        """)).rowcount

        if updated != expected_updates:
            session.rollback()
            print(f"REFUSING TO COMMIT: expected {expected_updates} updates, got {updated}.")
            return 2

        inserted = session.execute(text(f"""
            WITH resource_owners AS (
                {RESOURCE_OWNERS}
            )
            INSERT INTO resourceacl (mfid, accessgroup_id, read, write, permission)
            SELECT
                resource_owners.mfid,
                owner_group.id,
                TRUE,
                TRUE,
                CAST('owner' AS resource_role)
            FROM resource_owners
            JOIN accessgroup owner_group
              ON owner_group.group_name = resource_owners.owner_group_name
             AND owner_group.group_type::text = 'identity'
            WHERE resource_owners.owner_group_type = 'identity'
              AND resource_owners.resource_exists
              AND NOT EXISTS (
                  SELECT 1
                  FROM resourceacl existing
                  WHERE existing.mfid = resource_owners.mfid
                    AND existing.accessgroup_id = owner_group.id
              )
        """)).rowcount
        if inserted != counts.insertable_owner_acls:
            session.rollback()
            print(
                "REFUSING TO COMMIT: expected "
                f"{counts.insertable_owner_acls} inserts, got {inserted}."
            )
            return 2

        remaining_duplicates = session.execute(text("""
            SELECT count(*)
            FROM (
                SELECT mfid
                FROM resourceacl
                WHERE permission = 'owner'
                GROUP BY mfid
                HAVING count(*) > 1
            ) duplicate_resources
        """)).scalar_one()
        if remaining_duplicates:
            session.rollback()
            print(f"REFUSING TO COMMIT: {remaining_duplicates} duplicate owner resources remain.")
            return 2

        session.commit()
        print(f"committed: {updated} ACL rows updated, {inserted} owner ACLs inserted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
