"""Merge unambiguous legacy project and instrument groups into canonical MFID groups.

Only an unshared legacy project_id or instrument-name group with an existing canonical MFID group is eligible. The script refuses ambiguous mappings and conflicting owners. Membership and ACL conflicts preserve the stronger role. Join requests are repointed before the legacy group is removed.

The dry run is read-only. The commit uses a fixed number of set-based statements and works before or after the join-request and ACL column renames.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_merge_legacy_entity_groups.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_merge_legacy_entity_groups.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"
MAPPING_CTE = """
    mapping AS (
        SELECT
            legacy.id AS legacy_id,
            canonical.id AS canonical_id,
            legacy.group_name AS legacy_name,
            p.unique_id AS canonical_name,
            'project'::text AS group_type
        FROM project p
        JOIN accessgroup legacy ON legacy.group_name = p.project_id
        JOIN accessgroup canonical ON canonical.group_name = p.unique_id
        WHERE legacy.id <> canonical.id
          AND NOT EXISTS (SELECT 1 FROM "user" u WHERE lower(u.orcid) = lower(p.project_id))
          AND NOT EXISTS (SELECT 1 FROM instrument i WHERE lower(i.instrument_name) = lower(p.project_id))
          AND NOT EXISTS (
              SELECT 1 FROM project other
              WHERE other.id <> p.id AND lower(other.project_id) = lower(p.project_id)
          )
        UNION ALL
        SELECT
            legacy.id,
            canonical.id,
            legacy.group_name,
            i.unique_id,
            'instrument'::text
        FROM instrument i
        JOIN accessgroup legacy ON legacy.group_name = lower(i.instrument_name)
        JOIN accessgroup canonical ON canonical.group_name = i.unique_id
        WHERE legacy.id <> canonical.id
          AND NOT EXISTS (SELECT 1 FROM "user" u WHERE lower(u.orcid) = lower(i.instrument_name))
          AND NOT EXISTS (SELECT 1 FROM project p WHERE lower(p.project_id) = lower(i.instrument_name))
          AND NOT EXISTS (
              SELECT 1 FROM instrument other
              WHERE other.id <> i.id AND lower(other.instrument_name) = lower(i.instrument_name)
          )
    )
"""


def _columns(session: Session, table_name: str) -> set[str]:
    return set(session.execute(text("""
        SELECT column_name FROM information_schema.columns WHERE table_name = :table_name
    """), {"table_name": table_name}).scalars().all())


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
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (read only)'}")
        if database == PRODUCTION_DB and args.commit and not args.yes_this_is_production:
            print(f"REFUSING TO RUN: {database!r} is production.")
            return 2

        mappings = session.execute(text(f"""
            WITH {MAPPING_CTE}
            SELECT legacy_id, canonical_id, legacy_name, canonical_name, group_type
            FROM mapping
            ORDER BY group_type, legacy_name
        """)).all()
        duplicate_mappings = session.execute(text(f"""
            WITH {MAPPING_CTE}
            SELECT legacy_id, count(*)
            FROM mapping
            GROUP BY legacy_id
            HAVING count(*) > 1
        """)).all()
        if duplicate_mappings:
            print(f"REFUSING TO RUN: ambiguous legacy mappings: {duplicate_mappings!r}")
            return 2

        owner_conflicts = session.execute(text(f"""
            WITH {MAPPING_CTE}, owners AS (
                SELECT mapping.canonical_id, link.user_id
                FROM mapping
                JOIN useraccessgrouplink link
                  ON link.accessgroup_id IN (mapping.legacy_id, mapping.canonical_id)
                WHERE link.role = 'owner'
                GROUP BY mapping.canonical_id, link.user_id
            )
            SELECT canonical_id, count(*)
            FROM owners
            GROUP BY canonical_id
            HAVING count(*) > 1
        """)).all()
        if owner_conflicts:
            print(f"REFUSING TO RUN: canonical groups would have conflicting owners: {owner_conflicts!r}")
            return 2

        counts = session.execute(text(f"""
            WITH {MAPPING_CTE}
            SELECT
                (SELECT count(*) FROM mapping) AS groups,
                (SELECT count(*) FROM useraccessgrouplink link JOIN mapping ON link.accessgroup_id = mapping.legacy_id) AS memberships,
                (SELECT count(*) FROM resourceacl acl JOIN mapping ON acl.accessgroup_id = mapping.legacy_id) AS acls
        """)).one()
        print(f"groups        : {counts.groups}")
        print(f"memberships   : {counts.memberships}")
        print(f"ACL rows      : {counts.acls}")
        for mapping in mappings[:100]:
            print(f"  {mapping.group_type}: {mapping.legacy_name!r} -> {mapping.canonical_name!r}")
        if not args.commit or counts.groups == 0:
            print("dry run only: nothing written" if not args.commit else "nothing to do")
            return 0

        acl_columns = _columns(session, "resourceacl")
        acl_column = "permission" if "permission" in acl_columns else "role"
        join_columns = _columns(session, "accessgroupjoinrequest")

        session.execute(text(f"""
            WITH {MAPPING_CTE}
            INSERT INTO useraccessgrouplink (user_id, accessgroup_id, role)
            SELECT link.user_id, mapping.canonical_id, link.role
            FROM useraccessgrouplink link
            JOIN mapping ON mapping.legacy_id = link.accessgroup_id
            ON CONFLICT (user_id, accessgroup_id) DO UPDATE
            SET role = GREATEST(useraccessgrouplink.role, EXCLUDED.role)
        """))
        session.execute(text(f"""
            WITH {MAPPING_CTE}
            DELETE FROM useraccessgrouplink link
            USING mapping
            WHERE link.accessgroup_id = mapping.legacy_id
        """))

        session.execute(text(f"""
            WITH {MAPPING_CTE}
            UPDATE resourceacl canonical
            SET read = canonical.read OR legacy.read,
                write = canonical.write OR legacy.write,
                {acl_column} = GREATEST(canonical.{acl_column}, legacy.{acl_column})
            FROM mapping
            JOIN resourceacl legacy ON legacy.accessgroup_id = mapping.legacy_id
            WHERE canonical.accessgroup_id = mapping.canonical_id
              AND canonical.mfid = legacy.mfid
        """))
        session.execute(text(f"""
            WITH {MAPPING_CTE}
            DELETE FROM resourceacl legacy
            USING mapping, resourceacl canonical
            WHERE legacy.accessgroup_id = mapping.legacy_id
              AND canonical.accessgroup_id = mapping.canonical_id
              AND canonical.mfid = legacy.mfid
        """))
        session.execute(text(f"""
            WITH {MAPPING_CTE}
            UPDATE resourceacl acl
            SET accessgroup_id = mapping.canonical_id
            FROM mapping
            WHERE acl.accessgroup_id = mapping.legacy_id
        """))

        if "accessgroup_id" in join_columns:
            session.execute(text(f"""
                WITH {MAPPING_CTE}
                UPDATE accessgroupjoinrequest request
                SET accessgroup_id = mapping.canonical_id
                FROM mapping
                WHERE request.accessgroup_id = mapping.legacy_id
            """))
        elif "group_name" in join_columns:
            session.execute(text(f"""
                WITH {MAPPING_CTE}
                UPDATE accessgroupjoinrequest request
                SET group_name = mapping.canonical_name
                FROM mapping
                WHERE request.group_name = mapping.legacy_name
            """))
        else:
            session.rollback()
            print("REFUSING TO COMMIT: unknown accessgroupjoinrequest schema.")
            return 2

        session.execute(text(f"""
            WITH {MAPPING_CTE}
            UPDATE accessgroup canonical
            SET group_type = CAST(mapping.group_type AS access_group_type)
            FROM mapping
            WHERE canonical.id = mapping.canonical_id
        """))
        deleted = session.execute(text(f"""
            WITH {MAPPING_CTE}
            DELETE FROM accessgroup legacy
            USING mapping
            WHERE legacy.id = mapping.legacy_id
        """)).rowcount
        if deleted != counts.groups:
            session.rollback()
            print(f"REFUSING TO COMMIT: expected to remove {counts.groups} groups, removed {deleted}.")
            return 2
        session.commit()
        print(f"committed: {deleted} legacy groups merged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
