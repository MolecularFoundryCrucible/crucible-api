import argparse
import sys

from sqlmodel import Session


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("identity", help="Manage people's logins").add_subparsers(dest="command", required=True)
    move = commands.add_parser(
        "move",
        help="Move a login off an empty duplicate account onto the right one",
        description=(
            "For someone who signed in with a provider before linking it, which created a second, empty account. "
            "Moves the login only if that account holds nothing else, then switches it off. Accounts are never merged."
        ),
    )
    move.add_argument("--issuer", required=True, help="The provider's issuer, as listed in CRUCIBLE_AUTH_PROVIDERS")
    move.add_argument("--subject", required=True, help="The provider's subject for the login, such as an ORCID")
    move.add_argument("--to", required=True, dest="target", help="The right account: unique_id, linked ORCID, username or email")
    move.add_argument("--yes", action="store_true", help="Move without asking; required when there is no terminal")
    move.set_defaults(handler=_run_move)
    backfill = commands.add_parser(
        "backfill",
        help="Record a login for each person whose identifier is an ORCID; run once after upgrading from 3.x",
        description="Adds a user_identity row for each person whose identifier is an ORCID. A dry run unless --commit is given; safe to run again.",
    )
    backfill.add_argument("--issuer", required=True, help="The ORCID issuer these identifiers belong to, such as https://orcid.org")
    backfill.add_argument("--commit", action="store_true", help="Write the rows; without it nothing is changed")
    backfill.set_defaults(handler=_run_backfill)


def _run_move(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.auth.identity import find_identity
    from app.auth.linking import MoveRefused, move_identity
    from app.utils.user_utils import person_by_reference

    with Session(get_engine()) as session:
        identity = find_identity(session, args.issuer, args.subject)
        target = person_by_reference(session, args.target)
        if identity is None or target is None:
            print("No such login" if identity is None else f"No user matches {args.target}", file=sys.stderr)
            return 1
        if not args.yes:
            if not sys.stdin.isatty():
                print("Pass --yes to move without a terminal; nothing changed", file=sys.stderr)
                return 1
            if input(f"Move this login to {target.username or target.unique_id} and switch the account that has it off? [y/N]: ").strip().lower() not in {"y", "yes"}:
                print("Nothing changed", file=sys.stderr)
                return 1
        try:
            source = move_identity(session, identity, target)
        except MoveRefused as refused:
            session.rollback()
            print("Refused, nothing changed: " + "; ".join(refused.reasons), file=sys.stderr)
            return 1
        session.commit()
        print(f"Moved the login to {target.username or target.unique_id}; switched off {source.username or source.unique_id}", file=sys.stderr)
    return 0


def _run_backfill(args: argparse.Namespace) -> int:
    from app.auth.identity_backfill import backfill
    from app.tables import ORCID_ISSUERS

    if args.issuer not in ORCID_ISSUERS:
        print(f"--issuer must be one of: {', '.join(ORCID_ISSUERS)}")
        return 1
    counts = backfill(args.issuer, args.commit)
    new = f"added: {counts['new']}" if args.commit else f"dry run, would add: {counts['new']}"
    print(f"{new}, already linked: {counts['linked']}, not an ORCID: {counts['not_orcid']}, service accounts: {counts['service']}, conflicts: {counts['conflicts']}")
    return 1 if counts["conflicts"] else 0
