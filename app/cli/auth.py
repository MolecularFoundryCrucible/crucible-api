import argparse
import json
import sys


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("auth", help="Maintain sign-in records").add_subparsers(dest="command", required=True)
    cleanup = commands.add_parser(
        "cleanup",
        help="Remove expired sign-in records",
        description=(
            "Deletes expired Hub sign-in transactions, codes and tokens, sign-in history older than "
            "CRUCIBLE_AUTH_EVENT_RETENTION_DAYS (default 365), and accounts left waiting for approval "
            "30 days. Run it hourly. Prints one JSON line of counts per batch."
        ),
    )
    cleanup.add_argument("--dry-run", action="store_true", help="Report counts without deleting anything")
    cleanup.add_argument("--batch", type=int, default=1000, help="Most rows of each kind per batch (default 1000)")
    cleanup.add_argument("--batches", type=int, default=100, help="Most batches in one run (default 100)")
    cleanup.set_defaults(handler=_run_cleanup)
    end = commands.add_parser(
        "end-sessions",
        help="End signed-in sessions",
        description="Ends the signed-in sessions of one person (--user) or of everyone (--all), for example after a breach. A dry run unless --commit is given. API keys are not affected.",
    )
    who = end.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="unique_id, username, ORCID or email of the person")
    who.add_argument("--all", action="store_true", help="Everyone")
    end.add_argument("--commit", action="store_true", help="End them; without it only counts")
    end.set_defaults(handler=_run_end_sessions)
    revoke = commands.add_parser(
        "revoke-passwords",
        help="Remove every password, after a breach",
        description="Removes every password, cancels password links and ends the sessions of everyone who had one. People with another login keep it; the others need a new link (admin password-link). API keys are not affected. A dry run unless --commit is given.",
    )
    revoke.add_argument("--commit", action="store_true", help="Remove them; without it only counts")
    revoke.set_defaults(handler=_run_revoke_passwords)
    rewrite = commands.add_parser(
        "rewrite-issuer",
        help="Move logins to a provider's new issuer address",
        description="For a provider whose issuer address changed: list the new issuer in CRUCIBLE_AUTH_PROVIDERS and restart, then run this to move every login from the old issuer to the new one. Refuses if any login is already linked under the new issuer. A dry run unless --commit is given.",
    )
    rewrite.add_argument("--from", dest="old", required=True, help="The old issuer, exactly as stored")
    rewrite.add_argument("--to", dest="new", required=True, help="The new issuer, as listed in CRUCIBLE_AUTH_PROVIDERS")
    rewrite.add_argument("--commit", action="store_true", help="Move them; without it only counts")
    rewrite.set_defaults(handler=_run_rewrite_issuer)


def _run_revoke_passwords(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.auth.passwords import revoke_all_passwords

    counts = revoke_all_passwords(get_engine(), commit=args.commit)
    print(json.dumps({"mode": "commit" if args.commit else "dry-run", **counts}))
    return 0


def _run_rewrite_issuer(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.auth.identity import IssuerRewriteRefused, rewrite_issuer

    try:
        counts = rewrite_issuer(get_engine(), args.old, args.new, commit=args.commit)
    except IssuerRewriteRefused as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps({"mode": "commit" if args.commit else "dry-run", **counts}))
    return 0


def _run_end_sessions(args: argparse.Namespace) -> int:
    from sqlmodel import Session

    from app.api.dependencies import get_engine
    from app.auth.sessions import active_sessions, end_everyones_sessions, end_sessions
    from app.utils.user_utils import person_by_reference

    engine = get_engine()
    if args.all:
        count = end_everyones_sessions(engine, commit=args.commit)
        print(json.dumps({"mode": "commit" if args.commit else "dry-run", "sessions": count}))
        return 0
    with Session(engine) as session, session.begin():
        user = person_by_reference(session, args.user)
        if user is None:
            print("No such person", file=sys.stderr)
            return 1
        count = end_sessions(session, user, actor=None) if args.commit else len(active_sessions(session, user.id))
    print(json.dumps({"mode": "commit" if args.commit else "dry-run", "sessions": count}))
    return 0


def _run_cleanup(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.auth.cleanup import cleanup_sign_in_records

    if not 1 <= args.batch <= 10000 or not 1 <= args.batches <= 1000:
        print("--batch must be 1..10000 and --batches 1..1000", file=sys.stderr)
        return 1
    try:
        engine = get_engine()
    except Exception as error:
        print(f"The database is not usable: {error}", file=sys.stderr)
        return 1
    for _ in range(1 if args.dry_run else args.batches):
        counts = cleanup_sign_in_records(engine, commit=not args.dry_run, batch_size=args.batch)
        print(json.dumps({"mode": "dry-run" if args.dry_run else "commit", "counts": counts}))
        if not any(counts.values()):
            break
    return 0
