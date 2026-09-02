"""
Delete the admin and read_only ACL rows in batches.

721,231 rows on production, 61% of resourceacl. The backfill's step 3 does the
same thing in one statement, which is fine on staging (22,443 rows) and a long
exclusive lock plus a large WAL spike on production. Run this first; step 3
then finds nothing to do.

Commits after each batch, so locks are released between them and an interrupted
run leaves a consistent database. Re-running resumes.

ORDERING: the admin-group bypass in auth_utils must be DEPLOYED before this
runs. Until cutover, admin-group members reach every resource through these
rows, and on production all 8 of them are service accounts with no other group.

Dry run by default.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260807_delete_bypass_acl_rows.py
    DB_CONF_FILE=.env python migrations/data/20260807_delete_bypass_acl_rows.py --commit
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session

from app.api.dependencies import get_engine

PRODUCTION_DB = "crucible"
BYPASS_GROUPS = ("admin", "read_only")

COUNT_SQL = """
    SELECT ag.group_name, count(*) FROM resourceacl a
    JOIN accessgroup ag ON ag.id = a.accessgroup_id
    WHERE ag.group_name = ANY(:groups)
    GROUP BY 1 ORDER BY 2 DESC
"""

# ctid is the physical row address — the cheapest way to take an arbitrary
# bounded slice of matching rows without an ORDER BY over 1.1M of them.
DELETE_BATCH_SQL = """
    DELETE FROM resourceacl
    WHERE ctid IN (
        SELECT a.ctid FROM resourceacl a
        JOIN accessgroup ag ON ag.id = a.accessgroup_id
        WHERE ag.group_name = ANY(:groups)
        LIMIT :batch
    )
"""


def remaining(session):
    rows = session.execute(text(COUNT_SQL), {"groups": list(BYPASS_GROUPS)}).all()
    return {name: n for name, n in rows}, sum(n for _, n in rows)


def bypass_is_deployed():
    """Whether the running code honours admin-group membership directly.

    Deleting these rows without it takes every admin-group member offline.
    """
    try:
        from app.utils.auth_utils import _is_admin_by_group  # noqa: F401
        return True
    except ImportError:
        return False


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--commit", action="store_true",
                   help="apply; without it one batch is rolled back as a rehearsal")
    p.add_argument("--yes-this-is-production", action="store_true",
                   help=f"required to run against the {PRODUCTION_DB!r} database")
    p.add_argument("--batch-size", type=int, default=10_000)
    p.add_argument("--sleep", type=float, default=0.25,
                   help="seconds between batches, to leave room for other traffic")
    p.add_argument("--max-batches", type=int, default=None,
                   help="stop early; the rest can be done in a later run")
    args = p.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {actual!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (one batch, rolled back)'}")

        if actual == PRODUCTION_DB and not args.yes_this_is_production:
            print(f"\nREFUSING TO RUN: {actual!r} is production.")
            print("Re-run with --yes-this-is-production if that is intended.")
            return 2

        if not bypass_is_deployed():
            print("\nREFUSING TO RUN: auth_utils._is_admin_by_group is missing.")
            print("Deploy the admin-group bypass first, or every admin-group")
            print("member loses access the moment these rows go.")
            return 2

        by_group, total = remaining(session)
        if total == 0:
            print("\nnothing to delete")
            return 0

        print()
        for name, n in by_group.items():
            print(f"  {name:<12} {n:>9,}")
        print(f"  {'total':<12} {total:>9,}  in batches of {args.batch_size:,}")

        started = time.monotonic()
        deleted = batches = 0

        while True:
            if args.max_batches and batches >= args.max_batches:
                print(f"\nstopping after {batches} batches (--max-batches)")
                break

            n = session.execute(
                text(DELETE_BATCH_SQL),
                {"groups": list(BYPASS_GROUPS), "batch": args.batch_size},
            ).rowcount
            batches += 1
            deleted += n

            if not args.commit:
                session.rollback()
                print(f"\n  rehearsed one batch: {n:,} rows would be deleted")
                print(f"  rolled back — re-run with --commit to apply")
                return 0

            session.commit()

            elapsed = time.monotonic() - started
            rate = deleted / elapsed if elapsed else 0
            left = total - deleted
            eta = left / rate if rate else 0
            print(f"  batch {batches:>3}  deleted {deleted:>8,}/{total:,}"
                  f"  {rate:>7,.0f} rows/s  eta {eta/60:>5.1f} min")

            if n < args.batch_size:
                break
            if args.sleep:
                time.sleep(args.sleep)

        _, left = remaining(session)
        elapsed = time.monotonic() - started
        print(f"\ndeleted {deleted:,} rows in {batches} batches, {elapsed:.1f}s")
        print(f"remaining: {left:,}")
        if left:
            print("re-run to continue")
        else:
            print("\nConsider VACUUM ANALYZE resourceacl — this leaves dead tuples")
            print("behind and the planner's stats are now stale.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
