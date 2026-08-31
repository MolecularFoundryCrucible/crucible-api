"""Audit prerequisites for the additive Alembic revisions through 91f3c8a4d2e7.

This script is read-only and uses one database query.

Usage:
    DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20260831_audit_additive_alembic_preconditions.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


def main() -> int:
    engine = get_engine()
    with Session(engine) as session:
        row = session.execute(text("""
            SELECT
                current_database() AS database,
                (
                    SELECT count(*)
                    FROM project p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM "user" u WHERE u.orcid = p.project_lead_orcid
                    )
                ) AS dangling_project_leads,
                ARRAY(
                    SELECT p.project_id
                    FROM project p
                    WHERE NOT EXISTS (
                        SELECT 1 FROM "user" u WHERE u.orcid = p.project_lead_orcid
                    )
                    ORDER BY p.project_id
                    LIMIT 20
                ) AS dangling_project_sample,
                (SELECT count(*) FROM instrument WHERE unique_id IS NULL) AS null_instrument_mfids,
                ARRAY(
                    SELECT unique_id
                    FROM instrument
                    WHERE unique_id IS NOT NULL
                    GROUP BY unique_id
                    HAVING count(*) > 1
                    ORDER BY unique_id
                    LIMIT 20
                ) AS duplicate_instrument_mfids,
                to_regclass('public.alembic_version') IS NOT NULL AS alembic_version_table_exists
        """)).one()

        print(f"connected to  : {row.database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")
        print(f"legacy dangling project leads: {row.dangling_project_leads} (accepted)")
        if row.dangling_project_sample:
            print(f"  projects: {list(row.dangling_project_sample)!r}")
        print(f"null instrument MFIDs  : {row.null_instrument_mfids}")
        print(f"duplicate instrument MFIDs: {len(row.duplicate_instrument_mfids)}")
        if row.duplicate_instrument_mfids:
            print(f"  MFIDs: {list(row.duplicate_instrument_mfids)!r}")
        print(f"Alembic version table  : {'present' if row.alembic_version_table_exists else 'absent'}")

        if row.null_instrument_mfids or row.duplicate_instrument_mfids:
            print("BLOCKED: additive Alembic preconditions failed")
            return 1
        print("PASS: additive Alembic preconditions are satisfied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
