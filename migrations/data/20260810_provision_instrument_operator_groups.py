"""
Ensure every instrument has an operator access group keyed by its MFID
(unique_id), with an editor ACL entry on the instrument's own resource.

Before this, an instrument's group only came into existence lazily, inside
create_dataset, named after instrument_name.lower() — see
docs/permission_system_plan.md, "Binding a service account to an
instrument." POST /instruments now creates this group at registration time
(see app/api/routes/instruments.py); this backfill catches every instrument
registered before that shipped.

For each instrument missing a unique_id-named group:
  - if a legacy instrument_name.lower()-named group exists AND that name is
    not shared with anything else (a project, a user's ORCID, another
    instrument) -> RENAME it in place. accessgroup.id is unchanged, so every
    existing ResourceACL and UserAccessGroupLink row on it (including SA
    memberships) carries over automatically.
  - if no legacy group exists, or the legacy name is shared with something
    else (the documented "unknown" project/instrument collision) -> create a
    fresh group under the MFID and print a warning. A shared legacy name is
    left untouched; reconciling who should actually hold its grants is a
    manual call, not one this script should make.

Idempotent — an instrument that already has its MFID-named group with the
editor ACL is skipped. Dry run by default. Refuses to touch production
without --yes-this-is-production.

Usage:
    DB_CONF_FILE=.env python migrations/data/20260810_provision_instrument_operator_groups.py
    DB_CONF_FILE=.env python migrations/data/20260810_provision_instrument_operator_groups.py --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlalchemy import func, text
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup, Instrument, Project, User
from app.utils.permissions import Role
from app.utils.sql_utils import add_or_update, sql_create_resource_acl

PRODUCTION_DB = "crucible"


def name_is_shared_with_something_else(session, name: str, this_instrument_id: int) -> bool:
    if session.exec(select(Project).where(func.lower(Project.project_id) == name)).first():
        return True
    if session.exec(select(User).where(func.lower(User.unique_id) == name)).first():
        return True
    other = session.exec(
        select(Instrument).where(func.lower(Instrument.instrument_name) == name,
                                 Instrument.id != this_instrument_id)
    ).first()
    return other is not None


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

        instruments = session.exec(select(Instrument)).all()
        renamed, created, flagged = 0, 0, 0

        for inst in instruments:
            ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == inst.unique_id)).one_or_none()
            if ag is None:
                legacy_name = inst.instrument_name.lower()
                legacy_ag = session.exec(select(AccessGroup).where(AccessGroup.group_name == legacy_name)).one_or_none()
                if legacy_ag is not None and not name_is_shared_with_something_else(session, legacy_name, inst.id):
                    legacy_ag.group_name = inst.unique_id
                    session.add(legacy_ag)
                    ag = legacy_ag
                    renamed += 1
                    print(f"  renamed group {legacy_name!r} -> {inst.unique_id!r} (instrument {inst.instrument_name!r})")
                else:
                    ag = add_or_update(session, AccessGroup,
                                       AccessGroup.group_name == inst.unique_id,
                                       group_name=inst.unique_id)
                    created += 1
                    if legacy_ag is not None:
                        flagged += 1
                        print(f"  WARNING: group {legacy_name!r} is shared with something else — "
                             f"created fresh group {inst.unique_id!r} for instrument {inst.instrument_name!r}, "
                             f"leaving {legacy_name!r} untouched. Reconcile its members/grants manually.")
                    else:
                        print(f"  created fresh group {inst.unique_id!r} for instrument {inst.instrument_name!r}")
                if ag.owner_orcid is None and ag.resource_mfid is None:
                    ag.resource_mfid = inst.unique_id
                    session.add(ag)
                session.flush()

            sql_create_resource_acl(ag, inst.unique_id, read=True, write=True,
                                    session=session, role=Role.EDITOR)

        print(f"\n{renamed} renamed, {created} created fresh ({flagged} flagged for manual review), "
             f"{len(instruments) - renamed - created} already provisioned")

        if args.commit:
            session.commit()
            print("committed")
        else:
            session.rollback()
            print("dry run only: nothing written")

    return 0


if __name__ == "__main__":
    sys.exit(main())
