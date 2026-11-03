"""
Add a user_identity row for each person whose identifier is an ORCID. It only ever inserts, and is
safe to run again. Run after the code that writes these rows is deployed, so nobody is missed.
A new installation has no existing users and does not need it.

Usage (from inside the pod):
    /root/.venv/bin/python migrations/data/20261103_backfill_user_identity.py --issuer https://orcid.org
    /root/.venv/bin/python migrations/data/20261103_backfill_user_identity.py --issuer https://orcid.org --commit
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.auth.identity import IdentityTaken, ensure_identity, find_identity
from app.general_utils import is_orcid
from app.models.core_models import ORCID_ISSUERS, User

BATCH_SIZE = 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add a user_identity row for each person whose identifier is an ORCID. A dry run unless --commit is given.")
    parser.add_argument("--issuer", required=True, help="The ORCID issuer these identifiers belong to, such as https://orcid.org")
    parser.add_argument("--commit", action="store_true", help="Write the rows; without it nothing is changed")
    args = parser.parse_args(argv)
    if args.issuer not in ORCID_ISSUERS:
        print(f"--issuer must be one of: {', '.join(ORCID_ISSUERS)}")
        return 1
    counts = {"new": 0, "linked": 0, "not_orcid": 0, "service": 0, "conflicts": 0}
    last_id = 0
    while True:
        with Session(get_engine()) as session:
            users = session.exec(select(User).where(User.id > last_id).order_by(User.id).limit(BATCH_SIZE)).all()
            if not users:
                break
            for user in users:
                counts[backfill_user(session, user, args.issuer, args.commit)] += 1
            last_id = users[-1].id
            if args.commit:
                session.commit()
    new = f"added: {counts['new']}" if args.commit else f"dry run, would add: {counts['new']}"
    print(f"{new}, already linked: {counts['linked']}, not an ORCID: {counts['not_orcid']}, service accounts: {counts['service']}, conflicts: {counts['conflicts']}")
    return 1 if counts["conflicts"] else 0


def backfill_user(session: Session, user: User, issuer: str, commit: bool) -> str:
    if user.is_service_account:
        return "service"
    if not is_orcid(user.unique_id):
        return "not_orcid"
    existing = find_identity(session, issuer, user.unique_id)
    if existing is not None:
        return "linked" if existing.user_id == user.id else "conflicts"
    if not commit:
        return "new"
    try:
        ensure_identity(session, user, issuer, user.unique_id)
    except IdentityTaken:
        return "conflicts"
    return "new"


if __name__ == "__main__":
    sys.exit(main())
