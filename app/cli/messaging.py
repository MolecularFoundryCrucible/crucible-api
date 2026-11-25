import argparse
import json
import sys


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("messaging", help="Send work to the message queue").add_subparsers(dest="command", required=True)
    pending = commands.add_parser(
        "publish-pending",
        help="Send ingestion requests and postprocessing runs the message queue has not taken yet",
        description="Sends every ingestion request and postprocessing run still requested and unsent a minute after it was made, for example because RabbitMQ was down; one still unsent after a day is marked failed. Schedule it every minute, never two at once. Does nothing while INGESTION_ENABLED is false. Prints the counts; exits 1 when something is still waiting or failed.",
    )
    pending.set_defaults(handler=_run_publish_pending)


def _run_publish_pending(args: argparse.Namespace) -> int:
    from app.api.dependencies import get_engine
    from app.config import check_settings
    from app.services.outbox import publish_pending

    try:
        check_settings()
        counts = publish_pending(get_engine())
    except Exception as error:
        print(f"Not done: {error}", file=sys.stderr)
        return 1
    print(json.dumps(counts))
    return 1 if counts["waiting"] or counts["failed"] else 0
