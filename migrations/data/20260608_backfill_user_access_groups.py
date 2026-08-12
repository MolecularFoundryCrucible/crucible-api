"""
Backfill personal AccessGroup for every User that doesn't have one.

Each user should have an AccessGroup named after their ORCID and be a member
of it. This group is normally created during the ORCID OAuth login flow
(/user_apikey endpoint). Users created via older code paths or directly in the
DB may be missing it, causing 500 errors on dataset creation.

Usage (from inside the API pod):
    /root/.venv/bin/python migrations/data/20260608_backfill_user_access_groups.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlmodel import Session, select
from app.api.dependencies import get_engine
from app.models.core_models import AccessGroup, User, UserAccessGroupLink
from app.utils.sql_utils import add_or_update


def backfill():
    engine = get_engine()

    with Session(engine) as session:
        users = session.exec(select(User)).all()
        print(f"Found {len(users)} users total.")

        created = 0
        linked = 0
        already_ok = 0

        for user in users:
            orcid = user.unique_id

            ag = session.exec(
                select(AccessGroup).where(AccessGroup.group_name == orcid)
            ).one_or_none()

            if ag is None:
                ag = add_or_update(session, AccessGroup, AccessGroup.group_name == orcid, group_name=orcid)
                created += 1
                print(f"  CREATED AccessGroup for {orcid}")

            if user not in ag.users:
                ag.users.append(user)
                linked += 1
                print(f"  LINKED  {orcid} to their AccessGroup")
            else:
                already_ok += 1

        session.commit()

    print(f"\nDone. {created} groups created, {linked} memberships added, {already_ok} already correct.")


if __name__ == "__main__":
    backfill()
