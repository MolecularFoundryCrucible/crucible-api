import argparse


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("api-keys", help="API keys").add_subparsers(dest="command", required=True)
    copy = commands.add_parser(
        "import",
        help="Copy API keys from before 4.0 into the hashed key table; run after upgrading from 3.x",
        description=(
            "Keys from before 4.0 are kept in plain text in apitoken, which 4.0 no longer reads: until a key is copied, "
            "it does not authenticate. A dry run unless --commit is given; safe to run again."
        ),
    )
    copy.add_argument("--commit", action="store_true", help="Copy the keys; without it nothing is changed")
    copy.set_defaults(handler=_run_import)


def _run_import(args: argparse.Namespace) -> int:
    from sqlmodel import Session

    from app.api.dependencies import get_engine
    from app.auth.api_keys import import_plain_keys

    with Session(get_engine()) as session:
        counts = import_plain_keys(session, commit=args.commit)
        if args.commit:
            session.commit()
    print(", ".join(f"{name.replace('_', ' ')}: {count}" for name, count in counts.items()))
    return 0
