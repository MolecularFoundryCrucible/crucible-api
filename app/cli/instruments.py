import argparse
import json


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("instruments", help="Instrument membership and grants").add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Report instrument grants that do not match membership and dataset assignment")
    check.add_argument("--commit", action="store_true", help="Bring them in line, as the 4.0.0 migration does, then report again; "
                       "without it nothing is changed")
    check.set_defaults(handler=_run_check)


def _run_check(args: argparse.Namespace) -> int:
    """Exit 1 when a count is not zero, so a deployment check can fail on it."""
    from sqlmodel import Session

    from app.api.dependencies import get_engine
    from app.services.instrument_members import datasets_without_id_record, grant_problems, repair_grants

    with Session(get_engine()) as session:
        problems = grant_problems(session)
        print(json.dumps(problems, indent=2))
        if args.commit and any(problems.values()):
            repair_grants(session)
            session.commit()
            problems = grant_problems(session)
            print(json.dumps(problems, indent=2))
        orphans = datasets_without_id_record(session)
        if orphans:
            print(f"{orphans} datasets have no id record and can hold no grants; they are not counted above.")
    return 1 if any(problems.values()) else 0
