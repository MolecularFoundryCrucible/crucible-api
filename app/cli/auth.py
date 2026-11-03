import argparse

from sqlmodel import Session, select

from app.auth.identity import IdentityTaken, ensure_identity, find_identity
from app.general_utils import is_orcid
from app.models.core_models import ORCID_ISSUERS, User

BATCH_SIZE = 1000


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("auth", help="Manage sign-in records").add_subparsers(dest="command", required=True)
    backfill = commands.add_parser(
        "backfill-identities",
        help="Add a sign-in record for each person whose identifier is an ORCID",
        description="A dry run unless --commit is given. It only ever inserts, and is safe to run again.",
    )
    backfill.add_argument("--issuer", required=True, help="The ORCID issuer these identifiers belong to, such as https://orcid.org")
    backfill.add_argument("--commit", action="store_true", help="Write the records; without it nothing is changed")
    backfill.set_defaults(handler=_run_backfill)


def _run_backfill(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine

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
                counts[_backfill_user(session, user, args.issuer, args.commit)] += 1
            last_id = users[-1].id
            if args.commit:
                session.commit()
    new = f"added: {counts['new']}" if args.commit else f"dry run, would add: {counts['new']}"
    print(f"{new}, already linked: {counts['linked']}, not an ORCID: {counts['not_orcid']}, service accounts: {counts['service']}, conflicts: {counts['conflicts']}")
    return 1 if counts["conflicts"] else 0


def _backfill_user(session: Session, user: User, issuer: str, commit: bool) -> str:
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
