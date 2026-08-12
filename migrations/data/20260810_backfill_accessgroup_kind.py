"""
Backfill accessgroup.owner_orcid / accessgroup.resource_mfid for every
existing access group, inferred from group_name: a group whose name matches
a registered user's ORCID is that user's personal group; a group whose name
matches a project's or instrument's own resource is that resource's group.
Everything else (admin, public, orphans) gets neither — unclassifiable by
name alone, and that is the correct outcome, not a gap to fill in.

Purely additive (see the schema migration, migrations/versions/
20260810_..._add_accessgroup_owner_orcid_and_.py). Idempotent — only
touches rows where both columns are NULL, safe to re-run.

Dry run by default. Refuses to touch production without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260810_backfill_accessgroup_kind.py
    DB_CONF_FILE=.env python migrations/data/20260810_backfill_accessgroup_kind.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup, Resource, User

PRODUCTION_DB = "crucible"
RESOURCE_GROUP_TYPES = ("project", "instrument")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true",
                        help="apply; without it everything is rolled back")
    parser.add_argument("--yes-this-is-production", action="store_true",
                        help=f"required to run against the {PRODUCTION_DB!r} database")
    args = parser.parse_args()

    engine = get_engine()
    with Session(engine) as session:
        actual = session.execute(text("SELECT current_database()")).scalar()
        print(f"connected to  : {actual!r} on {engine.url.host}:{engine.url.port}")
        print(f"mode          : {'COMMIT' if args.commit else 'DRY RUN (rolled back)'}")

        if actual == PRODUCTION_DB and not args.yes_this_is_production:
            print(f"\nREFUSING TO RUN: {actual!r} is production.")
            print("Re-run with --yes-this-is-production if that is genuinely intended.")
            return 2

        groups = session.exec(
            select(AccessGroup).where(AccessGroup.owner_orcid == None,  # noqa: E711
                                      AccessGroup.resource_mfid == None)  # noqa: E711
        ).all()
        print(f"unclassified access groups: {len(groups)}")

        # Two bulk queries instead of up to 2 * len(groups) — the difference
        # between 3 round-trips and ~19,000 over a network tunnel.
        user_orcids = set(session.exec(select(User.unique_id)).all())
        resource_mfids = set(session.exec(
            select(Resource.mfid).where(Resource.resource_type.in_(RESOURCE_GROUP_TYPES))
        ).all())

        owners, resources, unclassified = 0, 0, 0
        for ag in groups:
            if ag.group_name in user_orcids:
                ag.owner_orcid = ag.group_name
                session.add(ag)
                owners += 1
            elif ag.group_name in resource_mfids:
                ag.resource_mfid = ag.group_name
                session.add(ag)
                resources += 1
            else:
                unclassified += 1

        print(f"  personal groups classified : {owners}")
        print(f"  resource groups classified : {resources}")
        print(f"  left unclassified          : {unclassified} (reserved names and orphans, expected)")

        dupes = session.execute(text(
            "SELECT owner_orcid, count(*) FROM accessgroup WHERE owner_orcid IS NOT NULL "
            "GROUP BY owner_orcid HAVING count(*) > 1"
        )).all()
        assert not dupes, f"a user would end up with more than one personal group: {dupes}"

        if args.commit:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run only: nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
