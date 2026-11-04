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

from sqlalchemy import insert
from sqlmodel import Session, select

from app.api.dependencies import get_engine
from app.auth.identity import retry_after_concurrent_first_write
from app.general_utils import is_orcid
from app.models.core_models import ORCID_ISSUERS, User, UserIdentity, get_utc_datetime

BATCH_SIZE = 1000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Add a user_identity row for each person whose identifier is an ORCID. A dry run unless --commit is given.")
    parser.add_argument("--issuer", required=True, help="The ORCID issuer these identifiers belong to, such as https://orcid.org")
    parser.add_argument("--commit", action="store_true", help="Write the rows; without it nothing is changed")
    args = parser.parse_args(argv)
    if args.issuer not in ORCID_ISSUERS:
        print(f"--issuer must be one of: {', '.join(ORCID_ISSUERS)}")
        return 1
    counts = dict.fromkeys(("new", "linked", "not_orcid", "service", "conflicts"), 0)
    last_id = 0
    while True:
        batch_counts, batch_last_id = retry_after_concurrent_first_write(lambda: backfill_batch(last_id, args.issuer, args.commit))
        if batch_last_id is None:
            break
        for name, count in batch_counts.items():
            counts[name] += count
        last_id = batch_last_id
    new = f"added: {counts['new']}" if args.commit else f"dry run, would add: {counts['new']}"
    print(f"{new}, already linked: {counts['linked']}, not an ORCID: {counts['not_orcid']}, service accounts: {counts['service']}, conflicts: {counts['conflicts']}")
    return 1 if counts["conflicts"] else 0


def backfill_batch(after_id: int, issuer: str, commit: bool) -> tuple[dict[str, int], int | None]:
    """Handle the next batch in one transaction, returning its counts and last user id, or no id when none are left."""
    counts = dict.fromkeys(("new", "linked", "not_orcid", "service", "conflicts"), 0)
    with Session(get_engine()) as session:
        users = session.exec(select(User.id, User.unique_id, User.is_service_account).where(User.id > after_id).order_by(User.id).limit(BATCH_SIZE)).all()
        if not users:
            return counts, None
        people = []
        for user_id, unique_id, is_service_account in users:
            if is_service_account:
                counts["service"] += 1
            elif not is_orcid(unique_id):
                counts["not_orcid"] += 1
            else:
                people.append((user_id, unique_id))
        holders = dict(session.exec(select(UserIdentity.subject, UserIdentity.user_id).where(UserIdentity.issuer == issuer, UserIdentity.subject.in_([subject for _, subject in people]))).all())
        with_orcid = set(session.exec(select(UserIdentity.user_id).where(UserIdentity.user_id.in_([user_id for user_id, _ in people]), UserIdentity.issuer.in_(ORCID_ISSUERS))).all())
        rows = []
        now = get_utc_datetime()
        for user_id, subject in people:
            if subject in holders:
                counts["linked" if holders[subject] == user_id else "conflicts"] += 1
            elif user_id in with_orcid:
                counts["conflicts"] += 1
            else:
                counts["new"] += 1
                rows.append({"user_id": user_id, "issuer": issuer, "subject": subject, "created_at": now})
        if commit and rows:
            session.execute(insert(UserIdentity).values(rows))
            session.commit()
    return counts, users[-1][0]


if __name__ == "__main__":
    sys.exit(main())
