"""Audit project and instrument slugs that differ only by letter case.

Run before making project_id lookup and uniqueness case-insensitive, or before adding case-insensitive unique indexes on project.project_id or instrument.instrument_id. Any reported group must be resolved first, because a case-insensitive lookup would match more than one row.

This script is read-only.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20261005_audit_case_insensitive_slugs.py
    DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20261005_audit_case_insensitive_slugs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


SLUG_COLUMNS = {
    "project": ("project", "project_id"),
    "instrument": ("instrument", "instrument_id"),
}


def find_case_collisions(session, kind: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """(lowercased slug, [(slug, unique_id), ...]) for every slug shared case-insensitively."""
    table, column = SLUG_COLUMNS[kind]
    rows = session.execute(text(f"""
        SELECT lower({column}) AS folded, {column} AS slug, unique_id
        FROM {table}
        WHERE lower({column}) IN (
            SELECT lower({column}) FROM {table}
            WHERE {column} IS NOT NULL
            GROUP BY lower({column})
            HAVING count(*) > 1
        )
        ORDER BY folded, slug, unique_id
    """)).all()
    groups: dict[str, list[tuple[str, str]]] = {}
    for row in rows:
        groups.setdefault(row.folded, []).append((row.slug, row.unique_id))
    return list(groups.items())


def main() -> int:
    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print("mode          : READ ONLY")

        found = False
        for kind in SLUG_COLUMNS:
            groups = find_case_collisions(session, kind)
            print(f"{kind} slug collisions: {len(groups)}")
            for folded, members in groups[:50]:
                listed = ", ".join(f"{slug!r} ({unique_id})" for slug, unique_id in members)
                print(f"  {folded!r}: {listed}")
            found = found or bool(groups)

        if found:
            print("RESULT: resolve the collisions above before enforcing case-insensitive slugs.")
            return 1
        print("RESULT: no case-only slug collisions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
