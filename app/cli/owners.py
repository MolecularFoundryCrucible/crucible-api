import argparse
import csv


def register(groups: argparse._SubParsersAction) -> None:
    commands = groups.add_parser("owners", help="Manage record owners").add_subparsers(dest="command", required=True)
    backfill = commands.add_parser(
        "backfill",
        help="Set the owner of datasets and samples written by 3.x; run once after upgrading from 3.x",
        description=(
            "Fills dataset and sample owner_user_id from owner_orcid where that names a user, so owner filters and owner "
            "fields find those records. A dry run unless --commit is given; safe to run again."
        ),
    )
    backfill.add_argument("--unmatched", help="Write the owners that name no user to this CSV file; it holds personal data, so keep it out of any repository")
    backfill.add_argument("--commit", action="store_true", help="Write owner_user_id; without it nothing is changed")
    backfill.set_defaults(handler=_run_backfill)


def _run_backfill(args: argparse.Namespace) -> int:
    from app.services.owner_backfill import KINDS, fill, unmatched_instruments

    unmatched, disagreements = [], 0
    for model, kind in KINDS:
        filled, missing, disagree = fill(model, kind, args.commit)
        unmatched += missing
        disagreements += disagree
        verb = "filled" if args.commit else "dry run, would fill"
        print(f"{kind}: {verb}: {filled}, owner names no user: {len(missing)}, owner_user_id and owner_orcid disagree: {disagree}")
    instruments = unmatched_instruments()
    unmatched += instruments
    print(f"instrument: owner names no user: {len(instruments)}")
    if args.unmatched:
        with open(args.unmatched, "w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("resource_type", "unique_id", "stored_owner"))
            writer.writerows(unmatched)
        print(f"wrote {len(unmatched)} unmatched owners to {args.unmatched}")
    return 1 if disagreements else 0
