"""
Backfill username for User rows where username is NULL and email is set.
Derives username from the email prefix (part before '@'), sanitizes it,
and deconflicts by appending _2, _3, ... on collision.

Usage (from inside the API pod):
    /root/.venv/bin/python migrations/backfill_username_from_email.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlmodel import Session, select
from app.api.dependencies import get_engine
from app.models.core_models import User
from app.utils.user_utils import sanitize_username_from_email


def _deconflict(candidate: str, taken: set) -> str:
    if candidate not in taken:
        return candidate
    n = 2
    while True:
        suffix = f"_{n}"
        base = candidate[:32 - len(suffix)]
        attempt = f"{base}{suffix}"
        if attempt not in taken:
            return attempt
        n += 1


def backfill():
    engine = get_engine()

    with Session(engine) as session:
        users_to_backfill = session.exec(
            select(User).where(User.username == None, User.email != None)
        ).all()

        if not users_to_backfill:
            print("No users to backfill.")
            return

        taken = set(session.exec(select(User.username).where(User.username != None)).all())
        print(f"Found {len(users_to_backfill)} users to backfill. {len(taken)} usernames already taken.")

        skipped = 0
        updated = 0

        for user in users_to_backfill:
            candidate = sanitize_username_from_email(user.email)
            if len(candidate) < 3:
                print(f"  SKIP {user.unique_id}: sanitized '{candidate}' too short (email: {user.email})")
                skipped += 1
                continue

            username = _deconflict(candidate, taken)
            taken.add(username)
            user.username = username
            session.add(user)
            updated += 1

        session.commit()

    print(f"Done. {updated} usernames set, {skipped} skipped (no valid prefix).")


if __name__ == "__main__":
    backfill()
