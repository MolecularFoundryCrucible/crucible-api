"""
Rename every project's own access group from being keyed by its human
project_id to its MFID (Project.unique_id) — closing the namespace-collision
privilege escalation described in docs/permission_system_plan.md,
"project_id collides with the access-group namespace." project_id remains
the project's public identifier (URL path segment, FK target for
Dataset.project_id/Sample.project_id); only the group's name changes.

HARD PREREQUISITE: every project must already have a real unique_id before
this runs — see migrations/data/20260810_backfill_project_unique_id.py. This
script refuses to proceed if any project is missing one, rather than
creating a group keyed by None.

HARD PREREQUISITE ON THE CODE SIDE: app/utils/project_utils.py,
app/api/routes/projects.py, app/api/routes/resources.py,
app/api/routes/access_groups.py, app/api/routes/users.py, and
app/utils/sql_utils.py must already be deployed with their unique_id-based
group lookups (this is what "C4b" changed) — those routes look a project's
group up by unique_id, so if this script hasn't renamed the groups yet, they
find nothing. Deploy schema -> backfill Project.unique_id -> run this script
-> deploy the C4b code, all before user traffic hits it. There is no safe
partial/gradual rollout: a half-migrated state breaks every project route.

For each project missing a unique_id-named group:
  - if a legacy project_id-named group exists AND that name is not shared
    with anything else (a user's ORCID, an instrument, another project) ->
    RENAME it in place. accessgroup.id is unchanged, so every existing
    ResourceACL and UserAccessGroupLink row on it carries over automatically.
  - if no legacy group exists, or the legacy name is shared with something
    else (the documented "unknown" project/instrument collision) -> create a
    fresh group under the MFID and print a warning. A shared legacy name is
    left untouched; reconciling who should actually hold its grants is a
    manual call, not one this script should make.

Idempotent — a project that already has its MFID-named group with the owner
ACL is skipped. Dry run by default. Refuses to touch production without
--yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260810_rename_project_groups_to_mfid.py
    DB_CONF_FILE=.env python migrations/data/20260810_rename_project_groups_to_mfid.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup, Instrument, Project, User
from app.utils.permissions import Role
from app.utils.sql_utils import sql_create_resource_acl

PRODUCTION_DB = "crucible"


def name_is_shared_with_something_else(name: str, this_project_id: int,
                                       user_orcids: set, instrument_names: set,
                                       project_ids_by_lower: dict) -> bool:
    """In-memory check against sets loaded once up front — the alternative
    (a query per project) is what made this script take 30+ minutes over a
    network tunnel for ~6,756 projects; see the accessgroup_kind backfill's
    history for the same lesson learned the slow way."""
    lname = name.lower()
    if lname in user_orcids:
        return True
    if lname in instrument_names:
        return True
    return any(pid != this_project_id for pid in project_ids_by_lower.get(lname, ()))


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

        missing_unique_id = session.exec(select(Project).where(Project.unique_id == None)).all()  # noqa: E711
        if missing_unique_id:
            print(f"\nREFUSING TO RUN: {len(missing_unique_id)} projects have no unique_id yet.")
            print("Run migrations/data/20260810_backfill_project_unique_id.py --commit first.")
            return 2

        projects = session.exec(select(Project)).all()

        # Bulk-load everything the per-project decisions need, once, instead
        # of up to 5 queries per project (up to ~34,000 round-trips for 6,756
        # projects over a network tunnel — see the accessgroup_kind backfill's
        # run for how long that actually takes in practice).
        existing_groups = {ag.group_name: ag for ag in session.exec(select(AccessGroup)).all()}
        user_orcids = {o.lower() for o in session.exec(select(User.unique_id)).all()}
        instrument_names = {n.lower() for n in session.exec(select(Instrument.instrument_name)).all()}
        project_ids_by_lower = {}
        for p in projects:
            project_ids_by_lower.setdefault(p.project_id.lower(), []).append(p.id)

        renamed, created, flagged = 0, 0, 0
        total = len(projects)

        for i, proj in enumerate(projects, 1):
            ag = existing_groups.get(proj.unique_id)
            if ag is None:
                legacy_ag = existing_groups.get(proj.project_id)
                if legacy_ag is not None and not name_is_shared_with_something_else(
                    proj.project_id, proj.id, user_orcids, instrument_names, project_ids_by_lower
                ):
                    legacy_ag.group_name = proj.unique_id
                    session.add(legacy_ag)
                    ag = legacy_ag
                    renamed += 1
                    print(f"  renamed group {proj.project_id!r} -> {proj.unique_id!r}")
                else:
                    ag = AccessGroup(group_name=proj.unique_id)
                    session.add(ag)
                    session.flush()  # ag.id, needed by sql_create_resource_acl below
                    created += 1
                    if legacy_ag is not None:
                        flagged += 1
                        print(f"  WARNING: group {proj.project_id!r} is shared with something else — "
                             f"created fresh group {proj.unique_id!r} for project {proj.project_id!r}, "
                             f"leaving {proj.project_id!r} untouched. Reconcile its members/grants manually.")
                    else:
                        print(f"  created fresh group {proj.unique_id!r} for project {proj.project_id!r}")
                if ag.owner_orcid is None and ag.resource_mfid is None:
                    ag.resource_mfid = proj.project_id
                    session.add(ag)

            # The project's ACL entry keys on Resource.mfid, which stays
            # project_id — only the group's name changed, not what it grants on.
            sql_create_resource_acl(ag, proj.project_id, read=True, write=True,
                                    session=session, role=Role.OWNER)

            if i % 500 == 0 or i == total:
                print(f"  ... {i}/{total} processed")

        print(f"\n{renamed} renamed, {created} created fresh ({flagged} flagged for manual review), "
             f"{total - renamed - created} already provisioned")

        if args.commit:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run only: nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
