"""Report noncanonical access groups and the state that must be reconciled.

This script is read-only and works before or after the access-group join-request and ACL column renames. It reports legacy project and instrument targets, ORCID-shaped orphans, memberships, ACLs, and pending join requests.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260827_audit_legacy_access_groups.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


def _column_names(session: Session, table_name: str) -> set[str]:
    return set(session.execute(text("""
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = :table_name
    """), {"table_name": table_name}).scalars().all())


def main() -> int:
    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")

        acl_columns = _column_names(session, "resourceacl")
        acl_column = "permission" if "permission" in acl_columns else "role"
        join_columns = _column_names(session, "accessgroupjoinrequest")
        if "accessgroup_id" in join_columns:
            join_predicate = "request.accessgroup_id = suspect.group_id"
        elif "group_name" in join_columns:
            join_predicate = "request.group_name = suspect.group_name"
        else:
            raise RuntimeError("AccessGroupJoinRequest has neither accessgroup_id nor group_name")

        sql = f"""
            WITH suspect AS (
                SELECT
                    ag.id AS group_id,
                    ag.group_name,
                    ARRAY(
                        SELECT p.unique_id
                        FROM project p
                        WHERE p.project_id = ag.group_name
                        ORDER BY p.unique_id
                    ) AS project_targets,
                    ARRAY(
                        SELECT i.unique_id
                        FROM instrument i
                        WHERE lower(i.instrument_name) = lower(ag.group_name)
                        ORDER BY i.unique_id
                    ) AS instrument_targets,
                    ag.group_name ~* '^\\d{{4}}-\\d{{4}}-\\d{{4}}-\\d{{3}}[\\dX]$' AS orcid_shaped
                FROM accessgroup ag
                WHERE ag.group_name NOT IN ('admin', 'public', 'read_only')
                  AND NOT EXISTS (SELECT 1 FROM "user" u WHERE u.orcid = ag.group_name)
                  AND NOT EXISTS (SELECT 1 FROM project p WHERE p.unique_id = ag.group_name)
                  AND NOT EXISTS (SELECT 1 FROM instrument i WHERE i.unique_id = ag.group_name)
            )
            SELECT
                suspect.group_name,
                suspect.project_targets,
                suspect.instrument_targets,
                suspect.orcid_shaped,
                COALESCE((
                    SELECT json_agg(
                        json_build_object(
                            'user_id', u.orcid,
                            'username', u.username,
                            'role', link.role::text
                        ) ORDER BY u.orcid
                    )
                    FROM useraccessgrouplink link
                    JOIN "user" u ON u.id = link.user_id
                    WHERE link.accessgroup_id = suspect.group_id
                ), '[]'::json) AS memberships,
                COALESCE((
                    SELECT json_agg(row_to_json(sample))
                    FROM (
                        SELECT acl.mfid, resource.resource_type, acl.{acl_column}::text AS permission
                        FROM resourceacl acl
                        JOIN idtype resource ON resource.mfid = acl.mfid
                        WHERE acl.accessgroup_id = suspect.group_id
                        ORDER BY acl.mfid
                        LIMIT 20
                    ) sample
                ), '[]'::json) AS acl_sample,
                (SELECT count(*) FROM resourceacl acl WHERE acl.accessgroup_id = suspect.group_id) AS acl_count,
                (SELECT count(*) FROM accessgroupjoinrequest request WHERE {join_predicate}) AS request_count
            FROM suspect
            ORDER BY suspect.group_name
        """
        rows = session.execute(text(sql)).all()
        print(f"noncanonical groups: {len(rows)}")
        for row in rows:
            hints = []
            if row.project_targets:
                hints.append(f"project targets={list(row.project_targets)!r}")
            if row.instrument_targets:
                hints.append(f"instrument targets={list(row.instrument_targets)!r}")
            if row.orcid_shaped:
                hints.append("ORCID-shaped orphan")
            if not hints:
                hints.append("custom candidate")
            print(f"\n{row.group_name!r}: {', '.join(hints)}")
            print(f"  memberships : {row.memberships!r}")
            print(f"  ACL count   : {row.acl_count}")
            print(f"  ACL sample  : {row.acl_sample!r}")
            print(f"  requests    : {row.request_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
