"""Provision one MFID-named operator group for every instrument.

Run after revision 91f3c8a4d2e7. A safe, unshared instrument-name group is renamed in place. Missing canonical groups are created. Existing canonical and unshared legacy groups block writes until reconciled.

The dry run is read-only. The commit uses set-based statements and works before or after the resourceacl role-to-permission rename.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260810_provision_instrument_operator_groups.py
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260810_provision_instrument_operator_groups.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


PRODUCTION_DB = "crucible"


def _acl_column(session: Session) -> str:
    columns = session.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'resourceacl'
          AND column_name IN ('role', 'permission')
        ORDER BY column_name
    """)).scalars().all()
    if len(columns) != 1:
        raise RuntimeError(f"Expected exactly one resourceacl role column, found {columns!r}")
    return columns[0]


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
            print("Re-run with --yes-this-is-production if that is intended.")
            return 2

        states = session.execute(text("""
            SELECT
                i.id,
                i.instrument_name,
                i.unique_id,
                canonical.id AS canonical_group_id,
                legacy.id AS legacy_group_id,
                EXISTS (
                    SELECT 1 FROM "user" u
                    WHERE lower(u.orcid) = lower(i.instrument_name)
                ) AS user_collision,
                EXISTS (
                    SELECT 1 FROM project p
                    WHERE lower(p.project_id) = lower(i.instrument_name)
                ) AS project_collision,
                EXISTS (
                    SELECT 1 FROM instrument other
                    WHERE other.id <> i.id
                      AND lower(other.instrument_name) = lower(i.instrument_name)
                ) AS instrument_collision
            FROM instrument i
            LEFT JOIN accessgroup canonical ON canonical.group_name = i.unique_id
            LEFT JOIN accessgroup legacy ON legacy.group_name = lower(i.instrument_name)
            ORDER BY i.instrument_name
        """)).all()

        unresolved = []
        rename_count = 0
        create_count = 0
        for state in states:
            shared = state.user_collision or state.project_collision or state.instrument_collision
            if state.canonical_group_id is not None:
                if state.legacy_group_id is not None and state.legacy_group_id != state.canonical_group_id and not shared:
                    unresolved.append(state)
            elif state.legacy_group_id is not None and not shared:
                rename_count += 1
            else:
                create_count += 1

        print(f"instruments   : {len(states)}")
        print(f"groups rename : {rename_count}")
        print(f"groups create : {create_count}")
        print(f"duplicates    : {len(unresolved)}")
        for state in unresolved[:50]:
            print(f"  {state.instrument_name!r}: canonical={state.unique_id!r}, legacy group remains")
        if unresolved:
            print("REFUSING TO RUN: reconcile duplicate legacy instrument groups first.")
            return 2

        acl_column = _acl_column(session)
        group_columns = set(session.execute(text("""
            SELECT column_name FROM information_schema.columns WHERE table_name = 'accessgroup'
        """)).scalars().all())
        legacy_kind_columns = {"owner_orcid", "resource_mfid"}.issubset(group_columns)
        print(f"ACL column    : {acl_column}")
        if not args.commit:
            print("dry run only: nothing written")
            return 0

        renamed = session.execute(text("""
            UPDATE accessgroup ag
            SET group_name = i.unique_id,
                group_type = CAST('instrument' AS access_group_type)
            FROM instrument i
            WHERE ag.group_name = lower(i.instrument_name)
              AND NOT EXISTS (
                  SELECT 1 FROM accessgroup canonical
                  WHERE canonical.group_name = i.unique_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM "user" u
                  WHERE lower(u.orcid) = lower(i.instrument_name)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM project p
                  WHERE lower(p.project_id) = lower(i.instrument_name)
              )
              AND NOT EXISTS (
                  SELECT 1 FROM instrument other
                  WHERE other.id <> i.id
                    AND lower(other.instrument_name) = lower(i.instrument_name)
              )
        """)).rowcount
        created = session.execute(text("""
            INSERT INTO accessgroup (group_name, group_type)
            SELECT i.unique_id, CAST('instrument' AS access_group_type)
            FROM instrument i
            WHERE NOT EXISTS (
                SELECT 1 FROM accessgroup ag WHERE ag.group_name = i.unique_id
            )
        """)).rowcount
        legacy_kind_assignment = ", resource_mfid = i.unique_id, owner_orcid = NULL" if legacy_kind_columns else ""
        legacy_kind_predicate = (
            " OR ag.resource_mfid IS DISTINCT FROM i.unique_id OR ag.owner_orcid IS NOT NULL"
            if legacy_kind_columns
            else ""
        )
        session.execute(text(f"""
            UPDATE accessgroup ag
            SET group_type = CAST('instrument' AS access_group_type)
                {legacy_kind_assignment}
            FROM instrument i
            WHERE ag.group_name = i.unique_id
              AND (
                  ag.group_type IS DISTINCT FROM CAST('instrument' AS access_group_type)
                  {legacy_kind_predicate}
              )
        """))
        acl_sql = f"""
            INSERT INTO resourceacl (mfid, accessgroup_id, read, write, {acl_column})
            SELECT i.unique_id, ag.id, TRUE, TRUE, CAST('editor' AS resource_role)
            FROM instrument i
            JOIN accessgroup ag ON ag.group_name = i.unique_id
            JOIN idtype resource ON resource.mfid = i.unique_id
                                AND resource.resource_type = 'instrument'
            ON CONFLICT (mfid, accessgroup_id) DO UPDATE
            SET {acl_column} = COALESCE(resourceacl.{acl_column}, EXCLUDED.{acl_column})
        """
        session.execute(text(acl_sql))

        if renamed != rename_count or created != create_count:
            session.rollback()
            print(
                "REFUSING TO COMMIT: database changed during the run "
                f"(expected rename/create {rename_count}/{create_count}, got {renamed}/{created})."
            )
            return 2
        session.commit()
        print(f"committed: {renamed} renamed, {created} created")
    return 0


if __name__ == "__main__":
    sys.exit(main())
