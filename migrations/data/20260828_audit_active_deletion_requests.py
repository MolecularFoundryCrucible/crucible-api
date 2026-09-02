"""Audit active deletion requests before installing their unique index.

This script is read-only. It exits with status 1 when a resource has more than one pending or approved deletion request.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20260828_audit_active_deletion_requests.py
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
        database = session.execute(text("SELECT current_database()")).scalar_one()
        duplicates = session.execute(text("""
            SELECT
                resource_id,
                count(*) AS active_requests,
                array_agg(id ORDER BY id) AS request_ids,
                array_agg(status ORDER BY id) AS statuses
            FROM deletionrequest
            WHERE status IN ('pending', 'approved')
            GROUP BY resource_id
            HAVING count(*) > 1
            ORDER BY resource_id
        """)).all()

        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")
        print(f"duplicate active deletion requests: {len(duplicates)}")
        for row in duplicates:
            print(
                f"  {row.resource_id!r}: count={row.active_requests}, "
                f"ids={row.request_ids!r}, statuses={row.statuses!r}"
            )

        if duplicates:
            print("BLOCKED: resolve duplicate active deletion requests before applying the index")
            return 1

        print("PASS: active deletion requests are unique per resource")
    return 0


if __name__ == "__main__":
    sys.exit(main())
