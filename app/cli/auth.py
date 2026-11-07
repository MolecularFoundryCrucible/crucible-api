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
    from app.utils.hub_auth_maintenance import cleanup_hub_auth

    if not 1 <= args.batch <= 10000 or not 1 <= args.batches <= 1000:
        print("--batch must be 1..10000 and --batches 1..1000", file=sys.stderr)
        return 1
    try:
        engine = get_engine()
    except Exception as error:
        print(f"The database is not usable: {error}", file=sys.stderr)
        return 1
    for _ in range(1 if args.dry_run else args.batches):
        counts = cleanup_hub_auth(engine, commit=not args.dry_run, batch_size=args.batch)
        print(json.dumps({"mode": "dry-run" if args.dry_run else "commit", "counts": counts}))
        if not any(counts.values()):
            break
    return 0
