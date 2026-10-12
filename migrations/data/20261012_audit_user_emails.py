"""Audit user emails before enforcing case-insensitive email uniqueness.

Reports emails shared by more than one user after trimming and lowercasing, and emails that are blank or padded with whitespace. Shared groups are listed by user identifier only; addresses are never printed. Every shared group must be resolved by hand before migration `e5a1c8d93f27` can create its unique index. Accounts are never merged or overwritten.

Read-only by default. `--commit` only normalizes stored values: blank emails become NULL and surrounding whitespace is trimmed.

Usage:
    DB_CONF_FILE=.env .venv/bin/python migrations/data/20261012_audit_user_emails.py
    DB_CONF_FILE=.env.prod .venv/bin/python migrations/data/20261012_audit_user_emails.py [--commit]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine


def shared_groups(session) -> list[list[str]]:
    rows = session.execute(text("""
        SELECT lower(btrim(email)) AS folded, orcid
        FROM "user"
        WHERE email IS NOT NULL AND btrim(email) <> ''
          AND lower(btrim(email)) IN (
              SELECT lower(btrim(email)) FROM "user"
              WHERE email IS NOT NULL AND btrim(email) <> ''
              GROUP BY lower(btrim(email))
              HAVING count(*) > 1
          )
        ORDER BY folded, orcid
    """)).all()
    groups: dict[str, list[str]] = {}
    for row in rows:
        groups.setdefault(row.folded, []).append(row.orcid)
    return list(groups.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit", action="store_true", help="normalize blank and whitespace-padded emails")
    args = parser.parse_args()
    engine = get_engine()
    with Session(engine) as session:
        database = session.execute(text("SELECT current_database()")).scalar_one()
        print(f"connected to  : {database!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'READ ONLY'}")
        total, with_email = session.execute(text('SELECT count(*), count(email) FROM "user"')).one()
        blank = session.execute(text("""SELECT count(*) FROM "user" WHERE email IS NOT NULL AND btrim(email) = ''""")).scalar_one()
        padded = session.execute(text("""SELECT count(*) FROM "user" WHERE btrim(email) <> '' AND email <> btrim(email)""")).scalar_one()
        groups = shared_groups(session)
        print(f"users         : {total} ({with_email} with an email value)")
        print(f"blank emails  : {blank}")
        print(f"padded emails : {padded}")
        print(f"shared emails : {len(groups)} groups, {sum(len(g) for g in groups)} users")
        for index, members in enumerate(groups[:50], 1):
            print(f"  group {index}: {', '.join(members)}")
        if args.commit:
            session.execute(text("""UPDATE "user" SET email = NULL WHERE email IS NOT NULL AND btrim(email) = ''"""))
            session.execute(text("""UPDATE "user" SET email = btrim(email) WHERE btrim(email) <> '' AND email <> btrim(email)"""))
            session.commit()
            print("normalized blank and padded emails")
        if groups:
            print("RESULT: resolve the shared emails above before enforcing uniqueness.")
            return 1
        print("RESULT: no shared emails.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
